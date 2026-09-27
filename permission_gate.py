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
    (r"\b(curl|wget)\b[^|]*\|\s*"
     r"(?:(?:sudo|doas|env|command|exec|nice|nohup|time)(?:\s+-\S+)*\s+|[A-Za-z_]\w*=\S*\s+)*"
     r"(?:\S*/)?(?:ba|z|da|k)?sh\b", "pipe download to shell"),
    (r">\s*/dev/(sd[a-z]|nvme\d|disk\d)", "write to raw device"),
    (r":\(\)\s*\{.*\|.*&.*\}\s*;", "fork bomb"),
)
# Force push, chmod 777 and history -c are judged on argv (see _argv_danger), so
# `echo git push --force` or a message that mentions one is not mistaken for it.

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

# Nesting deeper than this (sh -c inside $(...) inside eval ...) is denied rather
# than left unchecked. No ordinary command comes close.
MAX_DEPTH = 8
TOO_DEEP = "__too_deep__"

# Wrapper options that consume the next argument.
_WRAPPER_VALUE_OPTS = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T", "--user", "--group",
             "--chdir", "--prompt", "--role", "--type", "--other-user", "--close-from", "--host"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "--unset", "--chdir"},
    "nice": {"-n", "--adjustment"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "xargs": {"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a", "--max-args", "--max-procs",
              "--delimiter", "--arg-file", "--replace"},
}
# Reserved words that can precede a simple command; the command follows them.
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "(", "time"}

# Wrappers whose first positional argument is not the command: `timeout 5 cmd`.
_WRAPPER_POSITIONALS = {"timeout": 1}

# `NAME=value` before a command sets its environment; it is not the command.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _strip_comments(command: str) -> str:
    """Drop `# ...` to end of line where # starts a word outside quotes; the shell never runs it."""
    out, quote, i, n = [], None, 0, len(command)
    while i < n:
        c = command[i]
        if c == "\\" and quote != "'" and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
            continue
        if quote:
            quote = None if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or command[i - 1] in " \t\n;&|("):
            j = command.find("\n", i)
            i = n if j < 0 else j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _lift_substitutions(command: str, *, quotes: bool = True) -> tuple[str, list[str]]:
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
        if not quotes:                          # heredoc body: ' and " are ordinary text
            pass
        elif quote == "'":
            quote = None if c == "'" else quote
            out.append(c)
            i += 1
            continue
        if not quotes:
            pass
        elif c == "'" and quote is None:
            quote = "'"
        elif c == '"':
            quote = None if quote == '"' else '"'
        if c == "$" and command[i + 1:i + 2] == "(" and command[i + 2:i + 3] != "(":
            depth, j, inner = 1, i + 2, None
            while j < n and depth:
                ch = command[j]
                if ch == "\\" and inner != "'":
                    j += 2
                    continue
                if inner:
                    inner = None if ch == inner else inner
                elif ch in "'\"":
                    inner = ch
                else:
                    depth += {"(": 1, ")": -1}.get(ch, 0)
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
    """Drop leading environment assignments and wrappers (with their options): what runs is after them."""
    while argv:
        if _ASSIGNMENT.match(argv[0]) or argv[0] in _KEYWORDS:
            argv = argv[1:]
            continue
        wrapper = _basename(argv[0])
        if wrapper not in WRAPPERS:
            break
        argv = argv[1:]
        takes_value = _WRAPPER_VALUE_OPTS.get(wrapper, set())
        while argv and argv[0].startswith("-") and argv[0] != "-":
            flag = argv.pop(0)
            if flag == "--":
                break
            if flag in takes_value and argv:
                argv.pop(0)
        argv = argv[_WRAPPER_POSITIONALS.get(wrapper, 0):]
    return argv


def _shell_payload(argv: list[str]) -> str | None:
    """The command string a shell runs via -c, including combined flags such as `bash -lc`."""
    for j, arg in enumerate(argv[1:-1], start=1):
        if arg in ("-c", "/c", "-Command") or (
            arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]
        ):
            return argv[j + 1]
    return None


# A heredoc: `<<EOF`, `<<-'EOF'`, `<<"EOF"`. Group 3 is the body, up to the terminator line.
# A heredoc operator and its delimiter word: 'quoted', "quoted", \\escaped or bare.
_HEREDOC_OP = re.compile(r"""(?<!<)<<(-)?(?!<)[ \t]*(?:'([^'\n]+)'|"([^"\n]+)"|\\?([^\s;&|<>()'"\\]+))""")


def _strip_heredocs(command: str) -> tuple[str, list[str]]:
    """
    The command without heredoc bodies, plus the parts of those bodies that execute.

    A heredoc body is data (a PR description, a script for python3) unless a
    shell reads it as its script: `bash <<EOF` runs every line, while
    `bash -c 'cat' <<EOF` and `bash script.sh <<EOF` only read it. An unquoted
    delimiter still expands `$(...)` in the body, whatever reads it. A `<<`
    inside a comment starts nothing. The terminator must match exactly; only
    `<<-` lets it be indented, and only with tabs.
    """
    executed: list[str] = []
    out, pos, search = [], 0, 0
    while (m := _HEREDOC_OP.search(command, search)):
        line_start = command.rfind("\n", 0, m.start()) + 1
        prefix = command[line_start:m.start()]
        body_start = command.find("\n", m.end())
        if body_start < 0 or not _strip_comments(prefix + "<<").endswith("<<"):
            search = m.end()
            continue
        body_start += 1
        delim = m.group(2) or m.group(3) or m.group(4)
        quoted = bool(m.group(2) or m.group(3) or m.group(0).rstrip().endswith("\\" + delim))
        end, cursor = len(command), body_start
        while cursor < len(command):
            nl = command.find("\n", cursor)
            line = command[cursor:len(command) if nl < 0 else nl]
            if (line.lstrip("\t") if m.group(1) else line) == delim:
                end = cursor
                break
            cursor = len(command) if nl < 0 else nl + 1
        body = command[body_start:end]
        after = command.find("\n", end)
        after = len(command) if after < 0 else after

        segments = _segments(prefix) or [""]
        head_text = segments[-1]
        try:
            head = shlex.split(head_text)
        except ValueError:
            head = head_text.split()
        head = _skip_prefix(head)
        reads_script = (head and _basename(head[0]) in SHELLS and _shell_payload(head) is None
                        and not any(not a.startswith("-") for a in head[1:]))
        if reads_script:
            executed.append(body)
        elif not quoted:
            executed.extend(_lift_substitutions(body, quotes=False)[1])
        out.append(command[pos:body_start])
        pos = search = after
    out.append(command[pos:])
    return "".join(out), executed


def _segments(command: str, *, pipes: bool = False):
    """
    Split on ; & | && || and newlines, but never inside quotes. `2>&1` is not a separator.

    With pipes=True, returns (segment, piped) pairs, piped meaning the segment
    reads the previous one's output (`|` or `|&`, not `||`).
    """
    segments, cur, quote, i, piped = [], [], None, 0, False
    n = len(command)
    while i < n:
        c = command[i]
        if c == "\\" and quote != "'" and i + 1 < n:
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
            segments.append(("".join(cur), piped))
            cur = []
            nxt = command[i + 1:i + 2]
            if c == "|" and nxt == "|" or c == "&" and nxt == "&":
                piped, i = False, i + 1
            elif c == "|":
                piped = True
                if nxt == "&":
                    i += 1
            else:
                piped = False
        else:
            cur.append(c)
        i += 1
    segments.append(("".join(cur), piped))
    pairs = [(seg.strip(), was_piped) for seg, was_piped in segments if seg.strip()]
    return pairs if pipes else [seg for seg, _ in pairs]


def _mask_quotes(command: str) -> str:
    """
    The command with quoted prose removed.

    A quoted string with whitespace in it is an argument (a commit message, a
    grep pattern), not something the shell runs, so it becomes `_`. A quoted
    single word ("sh", "--force", "$HOME") keeps its text: it may still be a
    command, a flag or a target. Substitutions inside quotes are found by
    _lift_substitutions and scanned on their own.
    """
    out, quote, start, i = [], None, 0, 0
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
                out.append(body if body and not re.search(r"[\s;&|<>]", body) else "_")
                quote = None
        elif c in "'\"":
            quote, start = c, i + 1
        else:
            out.append(c)
        i += 1
    if quote:                                   # unterminated: treat the rest as code
        out.append(command[start - 1:])
    return "".join(out)


def executable_texts(command: str, *, _depth: int = 0) -> list[str]:
    """
    Every piece of this command line the shell will actually run, quoted prose removed.

    Checks run on these, so a commit message or PR body that *mentions* a
    download piped to a shell, or a force push, is not mistaken for one.
    Payloads that do execute (`sh -c '...'`, `eval`, `$(...)` and backticks,
    a heredoc fed to a shell) are scanned on their own.
    """
    if _depth > MAX_DEPTH:
        return [command]
    text, payloads = _strip_heredocs(command)
    text = _strip_comments(text)
    payloads += _lift_substitutions(text)[1]
    for argv in _argvs(text):
        name = _basename(argv[0])
        if name == "eval":
            payloads.append(" ".join(argv[1:]))
        elif name in SHELLS and (payload := _shell_payload(argv)) is not None:
            payloads.append(payload)
    texts = [_mask_quotes(text)]
    for payload in payloads:
        texts.extend(executable_texts(payload, _depth=_depth + 1))
    return texts


def _argvs(text: str) -> list[list[str]]:
    """The argv of each simple command in `text`, past keywords, assignments and wrappers."""
    return [argv for pipeline in _pipelines(text) for argv in pipeline]


def _pipelines(text: str) -> list[list[list[str]]]:
    """Commands grouped by pipeline, each as argv; `find -exec rm ... ;` adds the rm."""
    pipelines: list[list[list[str]]] = []
    for segment, piped in _segments(_lift_substitutions(text)[0], pipes=True):
        segment = segment.lstrip("({ \t").rstrip(") \t")    # `(rm x)`, `{ rm x; }`
        try:
            pieces = [shlex.split(segment)]
        except ValueError:
            # An unterminated quote: read it the way the old text check did, split
            # on every separator, so `echo it's; rm -rf ~` is still seen.
            pieces = []
            for piece in SEPARATORS.split(segment):
                try:
                    pieces.append(shlex.split(piece))
                except ValueError:
                    pieces.append(piece.split())
        stage: list[list[str]] = []
        for argv in pieces:
            argv = _skip_prefix(argv)
            if not argv:
                continue
            stage.append(argv)
            if _basename(argv[0]) == "find":
                for j, a in enumerate(argv):
                    if a in ("-exec", "-execdir", "-ok", "-okdir"):
                        rest = argv[j + 1:]
                        stop = next((k for k, b in enumerate(rest) if b in (";", "+")), len(rest))
                        inner = _skip_prefix(rest[:stop])
                        if inner:
                            stage.append(inner)
        if not stage:
            continue
        if piped and pipelines:
            pipelines[-1].extend(stage)
        else:
            pipelines.append(stage)
    return pipelines


def _pipe_to_shell(text: str) -> bool:
    """A download feeding a shell that reads its script from stdin: `curl x | sudo -u root sh`."""
    for pipeline in _pipelines(text):
        downloaded = False
        for argv in pipeline:
            name = _basename(argv[0])
            if name in ("curl", "wget"):
                downloaded = True
            elif downloaded and name in SHELLS and _shell_payload(argv) is None and not any(
                    not a.startswith("-") for a in argv[1:]):
                return True
    return False


# Delete targets that mean root, home or everything here.
_ROOT_TARGETS = {"/", "~", "$HOME", "${HOME}", "*"}


def _root_target(operand: str) -> bool:
    t = operand
    while t.endswith("/*") and t != "/*":
        t = t[:-2]
    if t == "/*":
        t = "/"
    t = t.rstrip("/") or "/"
    return t in _ROOT_TARGETS


def _argv_danger(argv: list[str]) -> str | None:
    """Irreversible by what the command's own arguments say."""
    name, args = _basename(argv[0]), argv[1:]
    if name == "rm":
        flags, operands, flags_done = [], [], False
        for a in args:
            if flags_done or not a.startswith("-") or a == "-":
                operands.append(a)
            elif a == "--":
                flags_done = True
            else:
                flags.append(a)
        recursive = any(f in ("-r", "-R", "--recursive") or (
            not f.startswith("--") and set(f[1:]) & {"r", "R"}) for f in flags)
        if recursive and any(_root_target(a) for a in operands):
            return "recursive delete of root, home or wildcard"
    if name == "git" and _subcommand(argv) == "push":
        for a in args:
            if a in ("--force", "-f") or a.startswith("+") or (
                    a.startswith("-") and not a.startswith("--") and "f" in a[1:]):
                return "dangerous construct: force push"
    mode = next((a for a in args if not a.startswith("-")), "")
    if name == "chmod" and re.fullmatch(r"0?777", mode):
        return "dangerous construct: chmod 777"
    if name == "history" and any(a.startswith("-") and "c" in a for a in args):
        return "dangerous construct: clear shell history"
    return None


def _dangerous(command: str, *, skip_find: bool = False) -> str | None:
    """Why this command is irreversible, judged only on what would actually execute; else None."""
    for text in executable_texts(command):
        for pattern, label in DANGEROUS_PATTERNS:
            if skip_find and label.startswith("find "):
                continue
            if re.search(pattern, text, re.IGNORECASE):
                return f"dangerous construct: {label}"
        for argv in _argvs(text):
            if (reason := _argv_danger(argv)):
                return reason
        if _pipe_to_shell(text):
            return "dangerous construct: pipe download to shell"
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
    if _depth > MAX_DEPTH:
        return [TOO_DEEP]

    found: list[str] = []
    text, bodies = _strip_heredocs(command)
    text, substitutions = _lift_substitutions(_strip_comments(text))
    for body in bodies + substitutions:
        found.extend(extract_commands(body, _depth=_depth + 1))
    for segment in _segments(text):
        segment = segment.lstrip("({ \t").rstrip(") \t")    # `(rm x)`, `{ rm x; }`
        if not segment:
            continue
        try:
            argv = shlex.split(segment)
        except ValueError:
            found.append("__unparseable__")
            continue
        if not argv:
            continue

        argv = _skip_prefix(argv)     # `FOO=bar sudo -u x npm run y` runs npm
        if not argv:
            continue
        name = _basename(argv[0])
        if name in SHELLS and (payload := _shell_payload(argv)) is not None:
            found.extend(extract_commands(payload, _depth=_depth + 1))   # not the shell's name
        elif name == "eval":
            found.extend(extract_commands(" ".join(argv[1:]), _depth=_depth + 1))
        else:
            found.append(name)
    return found or ["__empty__"]


def hard_block_reason(command: str) -> str | None:
    """A non-None reason means: never execute, never ask the model."""
    for binary in extract_commands(command):
        if binary in HARD_BLOCK:
            return f"blocked binary: {binary}"
        if binary == "__unparseable__":
            return "command could not be parsed safely"
    return _dangerous(command)


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
# rm is judged per operand in _argv_danger; this catches find walking root or home to delete.
_FIND_ROOT_DELETE = re.compile(
    r"\bfind\s+(?:/|~|\$HOME)(?=\s)[^|;&]*(?:-delete\b|-exec\s+rm\b)", re.IGNORECASE,
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
    """True when anything this command would run sends, publishes or deploys."""
    for text in executable_texts(command):
        for argv in _argvs(text):
            name = _basename(argv[0])
            if name in REMOTE or _subcommand(argv) in REMOTE_SUBCOMMANDS.get(name, ()):
                return True
    return False


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
    if TOO_DEEP in binaries:
        return "block", "nested too deeply to check"
    for binary in binaries:
        if binary in HARD_BLOCK - DELETES:
            return "block", f"blocked binary: {binary}"
    reason = _dangerous(command, skip_find=True)
    if reason:
        return "block", reason
    if any(_FIND_ROOT_DELETE.search(t) for t in executable_texts(command)):
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
