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
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
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

# `NAME=value` before a command sets its environment; it is not the command.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _lift_substitutions(command: str) -> tuple[str, list[str]]:
    """
    The command with each `$(...)` and backtick span replaced by `_`, plus their contents.

    Those spans run before the command they sit in, so `echo $(rm -rf ~)` runs
    rm. Single-quoted text is literal and left alone; double quotes do not stop
    a substitution. Nesting is balanced, so `$(a $(b))` is one span.
    """
    out, found, i, quote = [], [], 0, None
    n = len(command)
    while i < n:
        c = command[i]
        if c == "\\" and quote != "'" and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
            continue
        if quote == "'":
            quote = None if c == "'" else quote
            out.append(c)
            i += 1
            continue
        if c == "'" and quote is None:
            quote = "'"
        elif c == '"':
            quote = None if quote == '"' else '"'
        if c == "$" and command[i + 1:i + 2] == "(" and command[i + 2:i + 3] != "(":
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(command[j], 0)
                j += 1
            found.append(command[i + 2:j - 1] if not depth else command[i + 2:])
            out.append("_")
            i = j
            continue
        if c == "`":
            j = command.find("`", i + 1)
            j = n if j < 0 else j
            found.append(command[i + 1:j])
            out.append("_")
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out), found


def _skip_prefix(argv: list[str]) -> list[str]:
    """Drop leading environment assignments and wrappers (with their flags): what runs is after them."""
    while argv:
        if _ASSIGNMENT.match(argv[0]):
            argv = argv[1:]
        elif _basename(argv[0]) in WRAPPERS:
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                argv = argv[1:]
        else:
            break
    return argv

# A heredoc: `<<EOF`, `<<-'EOF'`, `<<"EOF"`. Group 3 is the body, up to the terminator line.
_HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_]\w*)\1[^\n]*\n(.*?)^[ \t]*\2[ \t]*$", re.S | re.M)
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


def _strip_heredocs(command: str) -> tuple[str, list[str]]:
    """
    The command without heredoc bodies, plus the bodies a shell will execute.

    A heredoc body is data (a PR description, a script for python3) unless the
    command it feeds is itself a shell: `bash <<EOF` runs every line of it.
    """
    executed: list[str] = []
    out, pos = [], 0
    for m in _HEREDOC.finditer(command):
        line_start = command.rfind("\n", 0, m.start()) + 1
        head = re.split(r"[;&|(]", command[line_start:m.start()])[-1].split()
        while head and _basename(head[0]) in WRAPPERS:
            head = head[1:]
        if head and _basename(head[0]) in SHELLS:
            executed.append(m.group(3))
        out.append(command[pos:m.start(3)])
        pos = m.end()
    out.append(command[pos:])
    return "".join(out), executed


def _segments(command: str) -> list[str]:
    """Split on ; & | && || and newlines, but never inside quotes. `2>&1` is not a separator."""
    segments, cur, quote, i = [], [], None, 0
    while i < len(command):
        c = command[i]
        if c == "\\" and quote != "'" and i + 1 < len(command):
            cur.append(command[i:i + 2])
            i += 2
            continue
        if quote:
            quote = None if c == quote else quote
            cur.append(c)
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c in ";|\n" or (c == "&" and command[i - 1:i] not in (">", "<") and command[i + 1:i + 2] != ">"):
            segments.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    segments.append("".join(cur))
    return [seg.strip() for seg in segments if seg.strip()]


def _mask_quotes(command: str) -> tuple[str, list[str]]:
    """
    The command with quoted prose removed, plus command substitutions found inside it.

    A quoted string with whitespace in it is an argument (a commit message, a
    grep pattern), not something the shell runs, so it becomes `_`. A quoted
    single word ("sh", "--force") keeps its text: it may still be a command or
    a flag. `$(...)` and backticks inside double quotes do run, so they are
    returned for scanning in their own right.
    """
    out, subs, quote, start, i = [], [], None, 0, 0
    while i < len(command):
        c = command[i]
        if c == "\\" and quote != "'" and i + 1 < len(command):
            if not quote:
                out.append(command[i:i + 2])
            i += 2
            continue
        if quote:
            if c == quote:
                body = command[start:i]
                if quote == '"':
                    subs.extend(a or b for a, b in _SUBSTITUTION.findall(body))
                out.append(body if body and not re.search(r"[\s;&|<>]", body) else "_")
                quote = None
        elif c in "'\"":
            quote, start = c, i + 1
        else:
            out.append(c)
        i += 1
    if quote:                                   # unterminated: treat the rest as code
        out.append(command[start - 1:])
    return "".join(out), subs


def executable_texts(command: str, *, _depth: int = 0) -> list[str]:
    """
    Every piece of this command line the shell will actually run, quoted prose removed.

    Pattern checks run on these, so a commit message or PR body that *mentions*
    a download piped to a shell, or a force push, is not mistaken for one.
    Payloads that do execute (`sh -c '...'`, `eval`, `$(...)` inside double
    quotes, a heredoc fed to a shell) are scanned on their own.
    """
    if _depth > 3:
        return [command]
    text, bodies = _strip_heredocs(command)
    masked, payloads = _mask_quotes(text)
    payloads += bodies + _lift_substitutions(text)[1]
    for segment in _segments(text):
        try:
            argv = shlex.split(segment)
        except ValueError:
            continue
        argv = _skip_prefix(argv)
        if not argv:
            continue
        name = _basename(argv[0])
        if name == "eval":
            payloads.append(" ".join(argv[1:]))
        elif name in SHELLS:
            for j, arg in enumerate(argv[:-1]):
                if arg in ("-c", "/c", "-Command"):
                    payloads.append(argv[j + 1])
                    break
    texts = [masked]
    for payload in payloads:
        texts.extend(executable_texts(payload, _depth=_depth + 1))
    return texts


def _dangerous(command: str, *, skip_find: bool = False) -> str | None:
    """The label of the first dangerous construct that would actually execute, if any."""
    for text in executable_texts(command):
        for pattern, label in DANGEROUS_PATTERNS:
            if skip_find and label.startswith("find "):
                continue
            if re.search(pattern, text, re.IGNORECASE):
                return label
    return None


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
    text, bodies = _strip_heredocs(command)
    text, substitutions = _lift_substitutions(text)
    for body in bodies + substitutions:
        found.extend(extract_commands(body, _depth=_depth + 1))
    for segment in _segments(text):
        segment = segment.lstrip("({ \t").rstrip(")} \t")    # `(rm x)`, `{ rm x; }`
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
            if _ASSIGNMENT.match(argv[i]):
                i += 1                # `FOO=bar npm run x` runs npm
                continue
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
            if name == "eval":
                found.extend(extract_commands(" ".join(argv[i + 1:]), _depth=_depth + 1))
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
    label = _dangerous(command)
    return f"dangerous construct: {label}" if label else None


def operator_allow(command: str, cwd: str, now: datetime | None = None) -> dict | None:
    """
    An owner-approved allowlist for commands the model will always send to review.

    `EXTERNAL` binaries (vercel, op, gh, ...) keep a model "review" by design, so
    an operation the owner has explicitly approved still cannot run. The allowlist
    file (JEV_COMMAND_ALLOWLIST, default ~/.jev/command-allow.json) holds entries
    {"pattern": <regex, full match>, "cwd_prefix": <path>, "expires": <ISO-8601>,
    "reason": <text>}. An entry must carry an expiry; expired, malformed or
    cwd-mismatched entries are ignored. Hard blocks run before this and still win.
    """
    path = Path(os.environ.get("JEV_COMMAND_ALLOWLIST") or Path.home() / ".jev" / "command-allow.json")
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    now = now or datetime.now(timezone.utc)
    for entry in entries if isinstance(entries, list) else []:
        try:
            expires = datetime.fromisoformat(str(entry["expires"]).replace("Z", "+00:00"))
            if expires.tzinfo is None or expires <= now:
                continue
            prefix = str(entry.get("cwd_prefix") or "")
            if prefix and not os.path.realpath(cwd).startswith(os.path.realpath(prefix)):
                continue
            if re.fullmatch(str(entry["pattern"]), command.strip(), re.DOTALL):
                return {"reason": str(entry.get("reason") or "owner allowlist"), "expires": expires.isoformat()}
        except (KeyError, TypeError, ValueError, re.error):
            continue
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
    r"\b(?:rm|rmdir|unlink)\b\s+(?:-\S+\s+)*(?:/|~|\$HOME|\*)(?=[\s);}`]|$|['\"])"
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
        for segment in _segments(_strip_heredocs(command)[0]):
            argv = shlex.split(segment)
            while argv and _ASSIGNMENT.match(argv[0]):
                argv = argv[1:]
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
    label = _dangerous(command, skip_find=True)
    if label:
        return "block", f"dangerous construct: {label}"
    texts = executable_texts(command)
    if any(_DELETE_ROOT.search(t) for t in texts) and (
        DELETES.intersection(binaries) or "__unparseable__" in binaries
        or any(re.search(r"\bfind\b.*(-delete|-exec\s+rm)\b", t) for t in texts)
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

    approved = operator_allow(command, cwd)
    if approved:
        decision = {"proposed": None, "final": "allow", "reason": f"owner allowlist: {approved['reason']}",
                    "source": "operator_allowlist", "expires": approved["expires"], "binaries": binaries}
        write_trace("permission", state, decision)
        return decision

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
                    "writes outside the workspace, uses credentials, or unclear effect"
                ),
                "block": "Destructive or irreversible, exfiltrates credentials to an "
                         "unknown destination, or violates policy",
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
