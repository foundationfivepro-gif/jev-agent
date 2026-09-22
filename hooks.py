#!/usr/bin/env python3
"""
Claude Code hooks — the half of jev-agent that runs whether or not the agent
remembers to ask.

The MCP tools are advisory: the agent calls them when it thinks to. A hook is
not. Claude Code runs these on every matching event, so the decision layer
applies to every prompt and every command in every session, not only inside
repositories whose CLAUDE.md says so.

    event                     subcommand    what it does
    UserPromptSubmit          prompt        one Jev call per prompt: complexity and
                                            whether repository context is needed,
                                            injected as a one-line note
    PreToolUse   Bash         gate-bash     jev_gate_command as a hook. The hard
                                            block is deterministic and needs no key.
    PreToolUse   Agent|Task   route-agent   jev_route_model as a hook. Sets `model`
                                            on a subagent that did not choose one.

Each subcommand reads Claude Code's hook JSON on stdin and writes hook JSON on
stdout. Silence means "no opinion" and the ordinary permission flow continues.

Three properties hold because this runs on every event:

  * It never exits non-zero. Exit 2 is Claude Code's blocking code, so a usage
    error or a crash here would block every prompt and every command.
  * It answers inside HOOK_BUDGET_S. A hook that outlives Claude Code's timeout
    is killed and the call proceeds ungated, so a slow Jev must resolve to an
    explicit `ask` before that happens, not after.
  * A `deny` cannot be overridden by the user, so it is reserved for the
    irreversible: dangerous constructs and recursive deletion of root, home or
    a wildcard. Anything else the deterministic layer dislikes becomes `ask`.

What leaves the machine: the prompt text (first MAX_TASK_CHARS) and the command
line go to Jev through the gateway. Anything credential-shaped is held back
and turned into `ask` without being sent.

    python3 hooks.py install              # ~/.claude/settings.json, ~/.claude/CLAUDE.md,
                                          # ~/.codex/AGENTS.md, ~/.codex/config.toml
    python3 hooks.py install --dry-run
    echo '{"tool_input":{"command":"rm -rf /"}}' | python3 hooks.py gate-bash

Hooks inherit Claude Code's environment, not the MCP server's, so the key is
read from this directory's .env (override with JEV_ENV_FILE). Traces go to
~/.jev/traces, never into the repository the session happens to be in.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("JEV_TRACE_DIR", str(Path.home() / ".jev" / "traces"))

MARK_BEGIN = "<!-- jev-agent:begin -->"
MARK_END = "<!-- jev-agent:end -->"
CODEX_MARK = "# jev-agent: registered by hooks.py install"
MAX_TASK_CHARS = 4000
HOOK_BUDGET_S = 20          # below Claude Code's 30s hook timeout, with margin
COMPLEXITY = ("mechanical", "standard", "multi-step", "frontier")

# `rm` targeting root, home or a bare wildcard is the one deletion that earns a
# hard deny. Every other rm is `ask`: the user sees it and decides.
RM_CATASTROPHIC = re.compile(
    r"\brm\b\s+(?:-\w+\s+)*(?:/|~|\$HOME|\*)(?=\s|$|['\"])", re.IGNORECASE
)


# ------------------------------------------------------------------ plumbing


def _load_env() -> None:
    path = Path(os.environ.get("JEV_ENV_FILE") or HERE / ".env")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _have_key() -> bool:
    _load_env()
    from core import active_transport

    return bool(active_transport())


def _read_stdin() -> dict:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()


def _pre_tool(decision: str | None, reason: str, updated: dict | None = None) -> dict:
    out: dict = {"hookEventName": "PreToolUse"}
    if decision:
        out["permissionDecision"] = decision
        out["permissionDecisionReason"] = reason
    if updated is not None:
        out["updatedInput"] = updated
    return {"hookSpecificOutput": out}


def _arm_budget(fallback: dict | None) -> None:
    """Answer before Claude Code gives up on us. Silence past the deadline is an ungated call."""
    if not hasattr(signal, "SIGALRM"):
        return

    def fire(_signum, _frame):
        if fallback is not None:
            _emit(fallback)
        print(f"jev: no answer within {HOOK_BUDGET_S}s", file=sys.stderr)
        os._exit(0)

    signal.signal(signal.SIGALRM, fire)
    signal.alarm(HOOK_BUDGET_S)


def _looks_secret(text: str) -> bool:
    from security_router import SECRET, classify

    return classify([], text)[0] == SECRET


# --------------------------------------------------------------------- hooks


def gate_bash(data: dict) -> None:
    from permission_gate import hard_block_reason

    command = str((data.get("tool_input") or {}).get("command") or "")
    if not command.strip():
        return

    blocked = hard_block_reason(command)
    if blocked:
        if blocked.startswith("dangerous construct"):
            _emit(_pre_tool("deny", f"jev: {blocked}"))
        elif blocked.startswith("blocked binary: rm") and RM_CATASTROPHIC.search(command):
            _emit(_pre_tool("deny", "jev: recursive delete of root, home or wildcard"))
        elif blocked.startswith(("blocked binary: rm", "blocked binary: unlink", "command could not")):
            _emit(_pre_tool("ask", f"jev: {blocked}"))
        else:
            _emit(_pre_tool("deny", f"jev: {blocked}"))
        return

    if _looks_secret(command):
        _emit(_pre_tool("ask", "jev: command contains credential-shaped material; not sent to Jev"))
        return

    # Unconfigured is not an outage: leave the ordinary permission flow alone.
    if not _have_key():
        return

    from core import TransportError
    from permission_gate import gate

    try:
        d = gate(command, str(data.get("cwd") or "."))
    except TransportError as exc:
        _emit(_pre_tool("ask", f"jev unreachable ({exc}); that is not permission to proceed"))
        return

    final, reason = d.get("final"), d.get("reason", "")
    if final == "block":
        _emit(_pre_tool("deny", f"jev: {reason}"))
    elif final == "review":
        _emit(_pre_tool("ask", f"jev: {reason}"))
    # allow: say nothing. A hook "allow" would bypass the user's own permission rules.


def route_agent(data: dict) -> None:
    tool_input = dict(data.get("tool_input") or {})
    if tool_input.get("model"):
        return  # an explicit choice is respected
    task = str(tool_input.get("prompt") or tool_input.get("description") or "").strip()
    if not task or _looks_secret(task) or not _have_key():
        return

    from core import TransportError
    from model_router import DEFAULT_CATALOG, route_model

    try:
        d = route_model(task[:MAX_TASK_CHARS])
    except TransportError as exc:
        print(f"jev route-agent: unreachable ({exc}); leaving model unset", file=sys.stderr)
        return

    selected = d["selected"]
    if selected == "human":
        _emit(_pre_tool("ask", "jev_route_model: no model should attempt this unaided"))
        return
    if selected not in DEFAULT_CATALOG:
        return
    confidence = d.get("confidence")
    reason = (
        f"jev_route_model: {selected} (proposed {d.get('proposed')}, "
        f"confidence {confidence:.2f})" if isinstance(confidence, float)
        else f"jev_route_model: {selected}"
    )
    _emit(_pre_tool("allow", reason, {**tool_input, "model": selected}))


def prompt(data: dict) -> None:
    text = data.get("prompt")
    if not isinstance(text, str):
        return
    text = text.strip()
    if len(text) < 20 or _looks_secret(text) or not _have_key():
        return

    from core import Noul, Score, TransportError, decide

    try:
        r = decide({"prompt": text[:MAX_TASK_CHARS]}, {
            "complexity": Score(
                instructions="Complexity of completing state.prompt as a coding task",
                criteria=list(COMPLEXITY),
            ),
            "repo": Noul(
                instructions="Completing state.prompt requires locating or reading code "
                             "in a repository",
            ),
        })
    except TransportError:
        return

    c = float(r.value("complexity"))
    needs_repo = float(r.answers["repo"].value) >= 0.5
    label = COMPLEXITY[max(0, min(3, round(c)))]
    note = (
        f"jev: complexity {label} ({c:.1f}/3); repository context "
        f"{'likely' if needs_repo else 'unlikely'} needed"
        + (" — call jev_select_context before reading files." if needs_repo else ".")
        + " Route subagents with jev_route_model; shell is gated by jev_gate_command."
    )
    _emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": note}})


# ------------------------------------------------------------------- install


def _backup(path: Path, dry_run: bool) -> Path | None:
    if not path.is_file() or dry_run:
        return None
    backup = path.with_name(f"{path.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    return backup


def _is_ours(hook: dict, hooks_path: str) -> bool:
    return hooks_path in (hook.get("args") or []) or hooks_path in str(hook.get("command", ""))


def _merge_hooks(existing: dict, ours: dict, hooks_path: str) -> dict:
    hooks = existing.setdefault("hooks", {})
    for event, groups in list(hooks.items()):
        kept = []
        for group in groups:
            remaining = [h for h in group.get("hooks", []) if not _is_ours(h, hooks_path)]
            if remaining:
                kept.append({**group, "hooks": remaining})
        hooks[event] = kept
    for event, groups in ours.items():
        hooks.setdefault(event, []).extend(groups)
    return existing


def _place_block(text: str, block: str) -> str:
    if MARK_BEGIN in text and MARK_END in text:
        head, _, rest = text.partition(MARK_BEGIN)
        _, _, tail = rest.partition(MARK_END)
        return head + block + tail
    return (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"


def install(args: argparse.Namespace) -> None:
    home = Path(args.home).expanduser()
    dry = args.dry_run
    verb = "would write" if dry else "wrote"
    py = sys.executable or "python3"
    hooks_path = str(HERE / "hooks.py")

    # The hooks will run under this interpreter. If it cannot import the
    # package, the hard block silently never fires — refuse now instead.
    try:
        import core  # noqa: F401
        import permission_gate  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            f"{py} cannot import jev-agent ({exc}). Install requirements into this "
            f"interpreter first: {py} -m pip install -r {HERE / 'requirements.txt'}"
        )

    def entry(sub: str, msg: str) -> dict:
        return {"type": "command", "command": py, "args": [hooks_path, sub],
                "timeout": 30, "statusMessage": msg}

    ours = {
        "UserPromptSubmit": [{"hooks": [entry("prompt", "jev: evaluating prompt")]}],
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [entry("gate-bash", "jev: gating command")]},
            {"matcher": "Agent|Task", "hooks": [entry("route-agent", "jev: routing subagent")]},
        ],
    }

    # Claude Code: hooks (user scope = every session on this machine).
    settings = home / ".claude" / "settings.json"
    existing: dict = {}
    if settings.is_file():
        try:
            existing = json.loads(settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{settings} is not valid JSON ({exc}); refusing to overwrite it.")
    merged = _merge_hooks(existing, ours, hooks_path)
    backup = _backup(settings, dry)
    if not dry:
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    print(f"hooks    — {verb} {settings}" + (f"  (backup {backup.name})" if backup else ""))
    print("             UserPromptSubmit -> prompt, PreToolUse Bash -> gate-bash, "
          "PreToolUse Agent|Task -> route-agent")

    # Policy block: Claude Code global memory and Codex global instructions.
    policy = (HERE / "CLAUDE.md").read_text(encoding="utf-8").strip()
    block = f"{MARK_BEGIN}\n{policy}\n{MARK_END}"
    for target in (home / ".claude" / "CLAUDE.md", home / ".codex" / "AGENTS.md"):
        if target.name == "AGENTS.md" and not target.parent.is_dir():
            print(f"policy   — skipped {target} (no Codex install)")
            continue
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
        backup = _backup(target, dry)
        if not dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_place_block(current, block), encoding="utf-8")
        print(f"policy   — {verb} {target}" + (f"  (backup {backup.name})" if backup else ""))

    # Codex: MCP registration, if Codex is installed and jev is not yet registered.
    codex_cfg = home / ".codex" / "config.toml"
    if codex_cfg.is_file():
        text = codex_cfg.read_text(encoding="utf-8")
        if "[mcp_servers.jev]" in text:
            print(f"codex    — {codex_cfg} already registers jev")
        else:
            _load_env()
            key = os.environ.get("AI_GATEWAY_API_KEY", "")
            table = (
                f"\n{CODEX_MARK}\n[mcp_servers.jev]\ncommand = \"{py}\"\n"
                f"args = [\"{HERE / 'mcp_server.py'}\"]\n"
                f"env = {{ AI_GATEWAY_API_KEY = \"{key}\" }}\nstartup_timeout_sec = 30\n"
            )
            backup = _backup(codex_cfg, dry)
            if not dry:
                codex_cfg.write_text(text.rstrip("\n") + "\n" + table, encoding="utf-8")
            print(f"codex    — {verb} [mcp_servers.jev] into {codex_cfg}"
                  + (f"  (backup {backup.name})" if backup else "")
                  + ("" if key else "  (AI_GATEWAY_API_KEY empty: set it in .env and re-run)"))
    else:
        print("codex    — no ~/.codex/config.toml; skipped MCP registration")

    print()
    print("Claude Code watches settings files it knew about at startup. If settings.json")
    print("did not exist when your session began, restart it or open /hooks once. Codex")
    print("reads AGENTS.md and config.toml on its next start.")


# ---------------------------------------------------------------------- main

HANDLERS = {"prompt": prompt, "gate-bash": gate_bash, "route-agent": route_agent}
FALLBACK = {
    "gate-bash": _pre_tool("ask", f"jev: no answer within {HOOK_BUDGET_S}s; that is not permission to proceed"),
    "route-agent": None,
    "prompt": None,
}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in HANDLERS:
        sub.add_parser(name)
    ins = sub.add_parser("install")
    ins.add_argument("--home", default="~", help="Home directory to install into (tests).")
    ins.add_argument("--dry-run", action="store_true")

    # argparse exits 2 on a usage error, and 2 is Claude Code's blocking code.
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            print("jev hooks: usage error; taking no position", file=sys.stderr)
        return

    if args.cmd == "install":
        install(args)
        return

    _arm_budget(FALLBACK[args.cmd])
    try:
        HANDLERS[args.cmd](_read_stdin())
    except Exception as exc:  # a hook must never take the session down with it
        print(f"jev {args.cmd}: {type(exc).__name__}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
