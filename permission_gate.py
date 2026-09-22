"""
Permission gate: allow, review or block one command before it reaches a shell.

The deterministic hard-block runs first and cannot be overridden by the model.
Jev then judges what survives. That ordering matters — the guide's version is
right about it — but the hard block is only as good as its command extraction,
and a naive `argv[0] in BLOCKLIST` is not.

    rm -rf /              blocked
    /bin/rm -rf /         passes      <- absolute path
    sudo rm -rf /         passes      <- wrapper
    bash -c 'rm -rf /'    passes      <- nested shell
    find . -delete        passes      <- no blocked binary at all

All four bypasses are closed here. The gate still fails closed: anything it
cannot parse becomes 'review' rather than 'allow'.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import PurePosixPath, PureWindowsPath
from typing import Iterable

from core import Choice, Noul, Score, decide, write_trace

# Binaries that are never run automatically, whatever Jev thinks.
#
# This list is deliberately narrow: irreversible or catastrophic only. Every
# false positive here is a reason for someone to switch the gate off, which
# costs far more safety than the marginal binary buys. Things that are merely
# risky — chmod, chown, curl, kill, package installs — are NOT hard-blocked;
# they go to Jev, which routes them to review. Specific dangerous *uses* of
# those (chmod 777, curl piped to a shell) are caught by DANGEROUS_PATTERNS.
HARD_BLOCK = {
    "rm", "rmdir", "unlink", "shred", "srm",
    "mkfs", "fdisk", "parted", "dd",
    "shutdown", "reboot", "halt", "poweroff", "init",
}

# Wrappers that run another command; the real command is what follows.
WRAPPERS = {"sudo", "doas", "env", "nice", "nohup", "time", "xargs", "timeout", "command", "exec", "builtin"}

# Shells whose -c argument is another command line to parse recursively.
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "powershell", "pwsh", "cmd"}

# Dangerous constructs that involve no blocked binary.
DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bfind\b[^|;]*-delete\b", "find -delete"),
    (r"\bfind\b[^|;]*-exec\b[^;]*\brm\b", "find -exec rm"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b", "pipe download to shell"),
    (r">\s*/dev/(sd[a-z]|nvme\d|disk\d)", "write to raw device"),
    (r":\(\)\s*\{.*\|.*&.*\}\s*;", "fork bomb"),
    (r"\bgit\b[^|;]*\bpush\b[^|;]*(--force(?!-with-lease)|-f)\b", "force push"),
    (r"\bchmod\b\s+(-[a-zA-Z]+\s+)*777\b", "chmod 777"),
    (r"\bhistory\s+-c\b", "clear shell history"),
)

SEPARATORS = re.compile(r"\s*(?:\|\||&&|[;&|])\s*")


def _basename(token: str) -> str:
    """Strip any path and extension so /usr/bin/rm and C:\\bin\\rm.exe both read as 'rm'."""
    name = PurePosixPath(token).name
    if "\\" in token:                       # Windows-style path
        name = PureWindowsPath(token).name
    return name.lower().removesuffix(".exe")


def extract_commands(command: str, *, _depth: int = 0) -> list[str]:
    """
    Every binary this command line would actually execute.

    Follows wrappers, recurses into `sh -c`, and splits on pipes and separators.
    Returns lowercased basenames. Bounded recursion — a line nested deeper than
    three shells is treated as unparseable by the caller.
    """
    if _depth > 3:
        return ["__unparseable__"]

    found: list[str] = []
    for segment in SEPARATORS.split(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            argv = shlex.split(segment)
        except ValueError:
            found.append("__unparseable__")
            continue
        if not argv:
            continue

        i = 0
        while i < len(argv):
            name = _basename(argv[i])
            if name in WRAPPERS:
                i += 1
                while i < len(argv) and argv[i].startswith("-"):
                    i += 1            # skip wrapper flags such as `env -i`
                continue
            if name in SHELLS:
                # Recurse into the -c payload rather than trusting the shell name.
                for j in range(i + 1, len(argv)):
                    if argv[j] in ("-c", "/c", "-Command"):
                        if j + 1 < len(argv):
                            found.extend(extract_commands(argv[j + 1], _depth=_depth + 1))
                        break
                else:
                    found.append(name)
                break
            found.append(name)
            break
    return found or ["__empty__"]


def hard_block_reason(command: str) -> str | None:
    """A non-None reason means: never execute, never ask the model."""
    for binary in extract_commands(command):
        if binary in HARD_BLOCK:
            return f"blocked binary: {binary}"
        if binary == "__unparseable__":
            return "command could not be parsed safely"
    for pattern, label in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return f"dangerous construct: {label}"
    return None


def gate(command: str, cwd: str = ".") -> dict:
    """
    Decide allow / review / block for one command.

    Order is deliberate: deterministic denial first, model judgement second,
    conservative conversion third. Jev can downgrade allow to review but can
    never upgrade a hard block.
    """
    if not command.strip():
        return {"final": "block", "reason": "empty command", "proposed": None}

    reason = hard_block_reason(command)
    if reason:
        decision = {"proposed": None, "final": "block", "reason": reason, "source": "policy"}
        write_trace("permission", {"command": command, "cwd": cwd}, decision)
        return decision

    binaries = extract_commands(command)
    state = {"command": command, "binaries": binaries, "cwd": cwd}

    result = decide(state, {
        "route": Choice(
            instructions="How should this command be handled?",
            criteria={
                "allow": "Read-only, reversible, confined to the workspace",
                "review": "Writes, network access, installs, or unclear effect",
                "block": "Destructive, touches credentials, or violates policy",
            },
        ),
        "impact": Score(
            instructions="Potential impact if this command is wrong",
            criteria=["none", "local and reversible", "broad", "irreversible"],
        ),
        "human": Noul(instructions="Must a human approve before this executes?"),
    })

    route = result.answers["route"]
    impact = float(result["impact"])
    human = float(result["human"])
    final = str(route.value)

    # Gate on the probability mass of the UNSAFE outcomes, not on confidence.
    #
    # A single confidence threshold is the obvious design and it is wrong here.
    # `pytest -q` comes back proposed=allow with P(block)=0, P(allow)=0.79 and
    # impact 0.75 — plainly safe — yet confidence 0.69 fails a 0.90 gate. A gate
    # that sends read-only commands to review teaches people to click through it,
    # which costs more safety than the strictness buys.
    #
    # The distribution answers the real question directly: how much belief is on
    # something destructive, and how bad is it if we are wrong.
    probs = route.probabilities or {}
    p_block = float(probs.get("block", 0.0))
    p_allow = float(probs.get("allow", 0.0))

    downgrade = None
    if final == "allow":
        if p_block > 0.05:
            downgrade = f"P(block)={p_block:.2f} > 0.05"
        elif impact >= 2:
            downgrade = f"impact {impact:.2f} >= 2 (broad or irreversible)"
        elif human >= 0.50:
            downgrade = f"human-approval probability {human:.2f} >= 0.50"
        elif p_allow < 0.60:
            downgrade = f"P(allow)={p_allow:.2f} < 0.60 (genuinely undecided)"
    if downgrade:
        final = "review"

    decision = {
        "proposed": route.value,
        "final": final,
        "reason": downgrade or "model route accepted",
        "source": "model",
        "confidence": route.certainty,
        "impact": impact,
        "human": human,
        "binaries": binaries,
        "probabilities": route.probabilities,
    }
    write_trace("permission", state, decision)
    return decision


if __name__ == "__main__":
    print(json.dumps(gate(" ".join(sys.argv[1:])), indent=2, default=str))
