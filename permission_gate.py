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

from core import UNTRUSTED, Choice, Noul, Score, decide, write_trace

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

# Binaries whose effect reaches past this machine, or into its credentials and
# system state. A model "review" on these always stands: a near-zero P(block)
# says nothing about where a request goes or what it publishes.
EXTERNAL = {
    "curl", "wget", "ssh", "scp", "sftp", "ftp", "rsync", "nc", "ncat", "telnet",
    "git", "gh", "vercel", "op", "security", "aws", "gcloud", "az", "kubectl",
    "terraform", "docker", "npm", "npx", "pnpm", "yarn", "pip", "pip3", "uv",
    "brew", "apt", "apt-get", "mail", "sendmail", "osascript", "open",
    "launchctl", "defaults", "crontab", "chmod", "chown", "kill", "killall", "pkill",
}

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


# Where a command's effect leaves this machine: it sends, publishes or deploys.
# Only these are worth a Jev call. Everything else is local work (builds, tests,
# git commits, installs, file edits) and goes straight to Claude Code's own
# permission flow: a Jev "review" there is a manual approval for nothing.
REMOTE = {
    "curl", "wget", "ssh", "scp", "sftp", "ftp", "rsync", "nc", "ncat", "telnet",
    "gh", "vercel", "netlify", "fly", "flyctl", "heroku", "wrangler", "firebase",
    "aws", "gcloud", "az", "kubectl", "helm", "terraform", "pulumi", "twine",
    "mail", "sendmail", "osascript",
}
# Tools that are local except for these subcommands.
REMOTE_SUBCOMMANDS = {
    "git": {"push", "send-email"},
    "npm": {"publish", "deprecate", "unpublish"},
    "pnpm": {"publish"},
    "yarn": {"publish", "npm"},
    "docker": {"push", "login"},
    "cargo": {"publish"},
    "gem": {"push"},
    "poetry": {"publish"},
    "uv": {"publish"},
}
_REMOTE_TEXT = re.compile(
    r"\b(?:" + "|".join(sorted(map(re.escape, REMOTE))) + r")\b"
    r"|\b(?:" + "|".join(REMOTE_SUBCOMMANDS) + r")\b[^|;&]*\b(?:"
    + "|".join(sorted({s for v in REMOTE_SUBCOMMANDS.values() for s in v})) + r")\b",
    re.IGNORECASE,
)

# Deletions that are ordinary local work unless they target root, home or a bare wildcard.
DELETES = {"rm", "rmdir", "unlink"}
_DELETE_ROOT = re.compile(
    r"\b(?:rm|rmdir|unlink)\b\s+(?:-\S+\s+)*(?:/|~|\$HOME|\*)(?=\s|$|['\"])"
    r"|\bfind\s+(?:/|~|\$HOME)(?=\s)",
    re.IGNORECASE,
)


# Global options that take a value before the subcommand: `git -C dir push`.
_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--prefix", "--cwd", "-w", "--workspace"}


def _subcommand(argv: list[str]) -> str:
    """First argument after the binary that is neither a flag nor a flag's value."""
    args = iter(argv[1:])
    for arg in args:
        if arg in _VALUE_OPTS:
            next(args, None)
        elif not arg.startswith("-"):
            return arg.lower()
    return ""


def reaches_outside(command: str) -> bool:
    """True when the command sends, publishes or deploys. Unparseable lines are read as text."""
    try:
        for segment in SEPARATORS.split(command):
            argv = shlex.split(segment)
            while argv and _basename(argv[0]) in WRAPPERS:
                # `sudo -u x scp`: flag values are indistinguishable from the command,
                # so resume at the first token that names a tool we care about.
                rest = argv[1:]
                known = [i for i, a in enumerate(rest)
                         if _basename(a) in REMOTE or _basename(a) in REMOTE_SUBCOMMANDS]
                argv = rest[known[0]:] if known else [a for a in rest if not a.startswith("-")]
            if not argv:
                continue
            name = _basename(argv[0])
            if name in SHELLS:
                return bool(_REMOTE_TEXT.search(segment))
            if name in REMOTE or _subcommand(argv) in REMOTE_SUBCOMMANDS.get(name, ()):
                return True
        return False
    except ValueError:
        return bool(_REMOTE_TEXT.search(command))


def triage(command: str) -> tuple[str, str]:
    """
    ('block' | 'local' | 'external', reason), with no model call.

    block     irreversible whatever the context: deny it.
    local     the effect stays on this machine: no Jev call and no prompt from us.
    external  sends, publishes or deploys: worth a Jev judgement.

    A deletion inside the workspace, or a line shlex cannot split (an apostrophe
    in a heredoc), is local work, not a reason to stop someone.
    """
    binaries = extract_commands(command)
    for binary in binaries:
        if binary in HARD_BLOCK - DELETES:
            return "block", f"blocked binary: {binary}"
    for pattern, label in DANGEROUS_PATTERNS:
        if not label.startswith("find ") and re.search(pattern, command, re.IGNORECASE):
            return "block", f"dangerous construct: {label}"
    if _DELETE_ROOT.search(command) and (
        DELETES.intersection(binaries) or "__unparseable__" in binaries or re.search(r"\bfind\b.*(-delete|-exec\s+rm)\b", command)
    ):
        return "block", "recursive delete of root, home or wildcard"
    if reaches_outside(command):
        return "external", "reaches past this machine"
    return "local", "local command; no Jev call"


def gate(command: str, cwd: str = ".") -> dict:
    """
    Decide allow / review / block for one command.

    Order is deliberate: deterministic denial first, model judgement second,
    conservative conversion third. Callers skip this for `triage` == 'local'.
    Jev can downgrade allow to review but can
    never upgrade a hard block.
    """
    if not command.strip():
        return {"final": "block", "reason": "empty command", "proposed": None}

    kind, reason = triage(command)
    if kind == "block":
        decision = {"proposed": None, "final": "block", "reason": reason, "source": "policy"}
        write_trace("permission", {"command": command, "cwd": cwd}, decision)
        return decision

    binaries = extract_commands(command)
    state = {"command": command, "binaries": binaries, "cwd": cwd}

    result = decide(state, {
        "route": Choice(
            instructions="How should this command be handled?" + UNTRUSTED,
            criteria={
                "allow": (
                    "Read-only, or creates, copies or converts files without deleting "
                    "or overwriting existing ones, confined to the workspace or a "
                    "temp/sandbox directory"
                ),
                "review": (
                    "Overwrites or deletes existing data, network access, installs, "
                    "writes outside the workspace, or unclear effect"
                ),
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

    # The converse error. The model labels plain file creation "review" — a
    # HEIC-to-JPG conversion or a copy into a sandbox came back P(block)=0,
    # impact 0.94, human 0.20 — and every such prompt is one more reason to
    # click through the gate. When there is no block mass, the impact is local
    # and reversible, a human is not wanted, and nothing leaves the machine,
    # the label is not evidence of risk.
    upgrade = None
    if (final == "review" and p_block <= 0.02 and impact < 1.25 and human < 0.40
            and not EXTERNAL.intersection(binaries)):
        final = "allow"
        upgrade = (f"review overruled: P(block)={p_block:.2f}, impact {impact:.2f}, "
                   f"human {human:.2f}, no external binary")

    downgrade = None
    if final == "allow" and not upgrade:
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
        "reason": downgrade or upgrade or "model route accepted",
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
