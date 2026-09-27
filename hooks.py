#!/usr/bin/env python3
"""
Claude Code hooks — the half of jev-agent that runs whether or not the agent
remembers to ask.

The MCP tools are advisory: the agent calls them when it thinks to. A hook is
not. Claude Code runs these on every matching event, so the decision layer
applies to every prompt and every command in every session, not only inside
repositories whose CLAUDE.md says so.

    event                     subcommand    what it does
    SessionStart              session       announces HOOKS_ACTIVE, so the policy (and
                                            any account-wide preference) can tell a
                                            hooked session from one with no hooks,
                                            such as Cowork. Local; no call.
    UserPromptSubmit          prompt        one Jev call per prompt; a one-line note
                                            only when repository context is needed.
                                            With `skill_router.py on` (default off),
                                            also names the skill Jev picked; 500ms cap.
    PreToolUse   Bash         gate-bash     denies the irreversible (no key needed);
                                            runs jev_gate_command only on commands that
                                            send, publish or deploy. Local commands get
                                            no Jev call and no prompt.
    PreToolUse   Agent|Task   route-agent   appends RETURN_CONTRACT to every subagent
                                            prompt (local), and sets `model` via
                                            jev_route_model when none was chosen.
    PostToolUse(Failure)      agent-outcome records how that subagent ended, keyed by
                 Agent|Task                 tool_use_id, so a route can be judged by
                                            what it produced. Local; no key, no call.

    python3 hooks.py report               # routes, fallbacks and outcomes so far

Each subcommand reads Claude Code's hook JSON on stdin and writes hook JSON on
stdout. Silence means "no opinion" and the ordinary permission flow continues.

Three properties hold because this runs on every event:

  * It never exits non-zero. Exit 2 is Claude Code's blocking code, so a usage
    error or a crash here would block every prompt and every command.
  * It answers inside HOOK_BUDGET_S. A slow Jev resolves to silence, and the
    user's own permission flow decides.
  * It never prompts. A hook `ask` overrides the user's allow rules and Bypass
    permissions, so Jev's verdicts are recorded, not enforced. The only output
    is a `deny`, reserved for the irreversible: dangerous constructs and
    recursive deletion of root, home or a wildcard.

What leaves the machine: the prompt text (first MAX_TASK_CHARS) and, for
commands that reach outside, the command line go to Jev through the gateway. Anything
credential-shaped is held back and not sent.

    python3 hooks.py install              # ~/.claude/settings.json, ~/.claude/CLAUDE.md
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
MAX_TASK_CHARS = 4000
HOOK_BUDGET_S = 20          # below Claude Code's 30s hook timeout, with margin
COMPLEXITY = ("mechanical", "standard", "multi-step", "frontier")

# The one signal that gating and routing are enforced here. Rules that apply on
# every surface (a claude.ai preference also reaches Cowork) key off this line
# instead of assuming hooks exist, so a hooked session never pays twice and an
# unhooked one never goes ungated.
HOOKS_ACTIVE = "jev hooks active"

# Appended to every subagent prompt. The marker makes it idempotent.
RETURN_MARK = "[jev return contract]"
RETURN_CONTRACT = (
    f"\n\n{RETURN_MARK} Reply with the conclusion only: findings, decision or change "
    "made, with file:line references. No file dumps, logs or restated task. Under 250 "
    "words unless the task above sets its own format."
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


def _bypassing(data: dict) -> bool:
    """The user chose Bypass permissions. An "ask" from us would override that choice."""
    return str(data.get("permission_mode") or "") == "bypassPermissions"


_FALLBACK: dict | None = None


def _arm_budget(fallback: dict | None) -> None:
    """Answer before Claude Code gives up on us. Silence past the deadline is an ungated call."""
    global _FALLBACK
    _FALLBACK = fallback
    if not hasattr(signal, "SIGALRM"):
        return

    def fire(_signum, _frame):
        if _FALLBACK is not None:
            _emit(_FALLBACK)
        print(f"jev: no answer within {HOOK_BUDGET_S}s", file=sys.stderr)
        os._exit(0)

    signal.signal(signal.SIGALRM, fire)
    signal.alarm(HOOK_BUDGET_S)


def _looks_secret(text: str) -> bool:
    from security_router import SECRET, classify

    return classify([], text)[0] == SECRET


# --------------------------------------------------------------------- hooks


def gate_bash(data: dict) -> None:
    from permission_gate import triage

    command = str((data.get("tool_input") or {}).get("command") or "")
    if not command.strip():
        return

    def note(reason: str) -> None:
        # Jev never prompts. A hook "ask" overrides the user's own allow rules, so its
        # opinion goes to stderr (and the trace) and Claude Code's permission flow decides.
        print(reason, file=sys.stderr)

    # Local work (builds, tests, commits, installs, deletes inside the workspace)
    # never reaches Jev. Claude Code's own permission rules decide everything
    # except the irreversible, which is denied here.
    kind, reason = triage(command)
    if kind == "block":
        _emit(_pre_tool("deny", f"jev: {reason}"))
        return
    if kind == "local":
        return

    if _looks_secret(command):
        note("jev: command contains credential-shaped material; not sent to Jev")
        return

    # Unconfigured is not an outage: leave the ordinary permission flow alone.
    if not _have_key():
        return

    from core import TransportError
    from permission_gate import gate

    try:
        d = gate(command, str(data.get("cwd") or "."))
    except TransportError as exc:
        note(f"jev unreachable ({exc})")
        return

    # A model verdict is judgement, not policy: it is recorded, never enforced. Only
    # the deterministic layer above may `deny`. Nor do we say "allow": that would
    # bypass the user's own permission rules.
    final, reason = d.get("final"), d.get("reason", "")
    if final in ("block", "review"):
        note(f"jev: {final}: {reason}")


def _with_contract(tool_input: dict) -> dict | None:
    """The subagent's input with RETURN_CONTRACT appended, or None if it already has it."""
    text = tool_input.get("prompt")
    if not isinstance(text, str) or not text.strip() or RETURN_MARK in text:
        return None
    return {**tool_input, "prompt": text.rstrip() + RETURN_CONTRACT}


def route_agent(data: dict) -> None:
    """
    Two jobs on every spawn. The first needs no key and no call.

    1. Append RETURN_CONTRACT to the subagent's prompt. Whatever a subagent
       returns is read by the parent at the parent's price and stays in its
       context for the rest of the session, so a short return is where
       delegation's saving is actually made or lost.
    2. If no model was chosen, route one with jev_route_model.
    """
    original = dict(data.get("tool_input") or {})
    tool_input = _with_contract(original) or original
    changed = tool_input is not original

    def contract_only(why: str) -> None:
        if changed:
            _emit(_pre_tool("allow", f"jev: return contract added ({why})", tool_input))

    if original.get("model"):
        contract_only("explicit model kept")
        return
    task = str(original.get("prompt") or original.get("description") or "").strip()
    if not task or _looks_secret(task) or not _have_key():
        contract_only("not routed")
        return

    from core import InvalidResponse, TransportError, write_trace
    from model_router import available_catalog, route_model

    cat = available_catalog()
    meta = _join_keys(data)
    try:
        d = route_model(task[:MAX_TASK_CHARS], catalog=cat, trace_meta=meta)
    except TransportError as exc:
        # Recorded, so an outage or a malformed answer shows up in `report`
        # instead of looking like a subagent that simply was not routed.
        reason = "invalid_response" if isinstance(exc, InvalidResponse) else "transport_error"
        write_trace("model_router", {"task": task[:MAX_TASK_CHARS]},
                    {"selected": None, "fallback": reason, "error": str(exc)[:300]}, meta=meta)
        print(f"jev route-agent: {reason} ({exc}); leaving model unset", file=sys.stderr)
        contract_only("Jev unavailable")
        return

    selected = d["selected"]
    if selected == "human":
        contract_only("human route; not enforced")
        return
    if selected not in cat:
        contract_only("no route")
        return
    confidence = d.get("confidence")
    reason = (
        f"jev_route_model: {selected} (proposed {d.get('proposed')}, "
        f"confidence {confidence:.2f})" if isinstance(confidence, float)
        else f"jev_route_model: {selected}"
    )
    _emit(_pre_tool("allow", reason, {**tool_input, "model": selected}))


def _join_keys(data: dict) -> dict:
    return {k: str(data[k]) for k in ("tool_use_id", "session_id") if data.get(k)}


def agent_outcome(data: dict) -> None:
    """
    Record how a subagent ended. Runs on PostToolUse and PostToolUseFailure.

    This is the half a routing decision cannot supply for itself: whether the
    model it chose came back with a result or an error. Only shape is stored —
    status, length, the model that ran — never the subagent's output, which is
    as sensitive as anything it read. Says nothing to Claude Code.
    """
    meta = _join_keys(data)
    if not meta.get("tool_use_id"):
        return
    from core import write_trace

    tool_input = data.get("tool_input") or {}
    response = data.get("tool_response")
    failed = data.get("hook_event_name") == "PostToolUseFailure" or bool(data.get("error"))
    if isinstance(response, dict) and response.get("is_error"):
        failed = True
    text = _response_text(response)
    # A subagent that returns nothing did not succeed, however cleanly it exited:
    # the check is on what came back, not on the router's confidence.
    status = "error" if failed else "empty" if not text.strip() else "ok"
    usage = response.get("usage") if isinstance(response, dict) else None
    tokens = response.get("totalTokens") if isinstance(response, dict) else None
    if tokens is None and isinstance(usage, dict):
        tokens = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    write_trace("model_router_outcome", meta, None, {
        "status": status,
        "model": tool_input.get("model"),
        "response_chars": len(text),
        "tokens": tokens,
    }, meta=meta)


def _response_text(response) -> str:
    """The text a subagent returned, from Claude Code's Agent tool_response shapes."""
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return ""
    content = response.get("content", response.get("result", ""))
    if isinstance(content, list):
        return "".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
    return str(content or "")


def report(trace_dir: Path | None = None) -> dict:
    """Join routing decisions to subagent outcomes. Read-only; prints nothing itself."""
    from collections import Counter

    root = Path(trace_dir or os.environ["JEV_TRACE_DIR"])
    routes: dict[str, dict] = {}
    outcomes: dict[str, dict] = {}
    unjoined = 0
    for path in sorted(root.glob("model_router*.json")) if root.is_dir() else []:
        try:
            t = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        key = (t.get("meta") or {}).get("tool_use_id")
        if t.get("system") == "model_router_outcome":
            if key:
                outcomes[key] = t.get("result") or {}
        elif key:
            routes[key] = t.get("decision") or {}
        else:
            unjoined += 1

    from model_router import DEFAULT_CATALOG

    by_model: dict[str, Counter] = {}
    tokens_by_model: Counter = Counter()
    fallbacks: Counter = Counter()
    jev_input = 0
    for key, d in routes.items():
        fallbacks[d.get("fallback") or "accepted"] += 1
        jev_input += int(d.get("input_tokens") or 0)
        model = str(d.get("selected"))
        outcome = outcomes.get(key) or {}
        by_model.setdefault(model, Counter())[outcome.get("status", "no_outcome")] += 1
        tokens_by_model[model] += int(outcome.get("tokens") or 0)

    # The bill that matters is per task that came back with something, not per
    # decision: a cheap route that fails costs its tokens plus the retry.
    cost_per_ok = {}
    for model, counts in by_model.items():
        ok, spent = counts.get("ok", 0), tokens_by_model[model]
        rate = (DEFAULT_CATALOG.get(model) or {}).get("cost_in")
        if ok and spent:
            cost_per_ok[model] = {
                "tokens_per_ok": spent // ok,
                "usd_per_ok_at_input_rate": round(spent / ok * rate / 1e6, 4) if rate else None,
            }
    jev_rate = float(os.environ.get("JEV_PRICE_PER_MTOK", "0.042"))
    explicit = sum(1 for k in outcomes if k not in routes)
    latencies = sorted(d["latency_ms"] for d in routes.values() if d.get("latency_ms") is not None)
    return {
        "routed": len(routes),
        "fallbacks": dict(fallbacks),
        "outcomes_by_selected_model": {m: dict(c) for m, c in sorted(by_model.items())},
        "cost_per_completed_subagent": dict(sorted(cost_per_ok.items())),
        "jev_routing_cost_usd": round(jev_input * jev_rate / 1e6, 6),
        "subagents_not_routed_by_jev": explicit,
        "decisions_without_tool_use_id": unjoined,
        "median_latency_ms": latencies[len(latencies) // 2] if latencies else None,
    }


def session(data: dict) -> None:
    """SessionStart (startup, resume, clear, compact): say, in one line, what the hooks enforce."""
    routing = "subagents routed" if _have_key() else "routing off (no key)"
    note = f"{HOOKS_ACTIVE}: Bash gated, {routing}, subagent return contract on."
    _emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": note}})


def prompt(data: dict) -> None:
    text = data.get("prompt")
    if not isinstance(text, str):
        return
    text = text.strip()
    if len(text) < 20 or _looks_secret(text) or not _have_key():
        return

    notes = []
    skill = _skill_note(text, data)
    if skill:
        notes.append(skill)

    from core import UNTRUSTED, Noul, Score, TransportError, decide

    try:
        r = decide({"prompt": text[:MAX_TASK_CHARS]}, {
            "complexity": Score(
                instructions="Complexity of completing state.prompt as a coding task" + UNTRUSTED,
                criteria=list(COMPLEXITY),
            ),
            "repo": Noul(
                instructions="Completing state.prompt requires locating or reading code "
                             "in a repository",
            ),
        })
    except TransportError:
        r = None

    # Injected text stays in context for the rest of the session, so the note
    # is sent only when it changes what Claude does next: read files through
    # jev_select_context rather than one by one.
    if r is not None and float(r.answers["repo"].value) >= 0.5:
        label = COMPLEXITY[max(0, min(3, round(float(r.value("complexity")))))]
        notes.append(f"jev: {label} task; call jev_select_context before reading files.")
    if notes:
        _emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                      "additionalContext": "\n".join(notes)}})


def _skill_note(text: str, data: dict) -> str | None:
    """One line naming the skill Jev picked, when the switch is on (`skill_router.py on`)."""
    try:
        import skill_router

        if not skill_router.enabled():
            return None
        project = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR")
        return skill_router.note(skill_router.route_skill(text, project=project))
    except Exception as exc:  # a suggestion is never worth a failed prompt
        print(f"jev skill router: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


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
        "SessionStart": [{"hooks": [entry("session", "jev: hooks active")]}],
        "UserPromptSubmit": [{"hooks": [entry("prompt", "jev: evaluating prompt")]}],
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [entry("gate-bash", "jev: gating command")]},
            {"matcher": "Agent|Task", "hooks": [entry("route-agent", "jev: routing subagent")]},
        ],
        "PostToolUse": [
            {"matcher": "Agent|Task", "hooks": [entry("agent-outcome", "jev: recording outcome")]},
        ],
        "PostToolUseFailure": [
            {"matcher": "Agent|Task", "hooks": [entry("agent-outcome", "jev: recording outcome")]},
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
    print("             SessionStart -> session, UserPromptSubmit -> prompt, PreToolUse Bash -> gate-bash, "
          "PreToolUse Agent|Task -> route-agent, PostToolUse(Failure) Agent|Task -> agent-outcome")

    # Policy block: Claude Code global memory. Installed with the hooks because
    # the policy tells Claude the hooks exist; one without the other would lie.
    policy = (HERE / "CLAUDE.md").read_text(encoding="utf-8").strip()
    block = f"{MARK_BEGIN}\n{policy}\n{MARK_END}"
    target = home / ".claude" / "CLAUDE.md"
    current = target.read_text(encoding="utf-8") if target.is_file() else ""
    backup = _backup(target, dry)
    if not dry:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_place_block(current, block), encoding="utf-8")
    print(f"policy   — {verb} {target}" + (f"  (backup {backup.name})" if backup else ""))

    print()
    print("Claude Code watches settings files it knew about at startup. If settings.json")
    print("did not exist when your session began, restart it or open /hooks once.")


# ---------------------------------------------------------------------- main

HANDLERS = {"prompt": prompt, "gate-bash": gate_bash, "route-agent": route_agent,
            "agent-outcome": agent_outcome, "session": session}
FALLBACK = {
    "agent-outcome": None,
    "session": None,
    "gate-bash": None,
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
    rep = sub.add_parser("report")
    rep.add_argument("--dir", default=None, help="Trace directory (default: $JEV_TRACE_DIR).")

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
    if args.cmd == "report":
        print(json.dumps(report(Path(args.dir) if args.dir else None), indent=2))
        return

    global _FALLBACK
    _arm_budget(FALLBACK[args.cmd])
    try:
        data = _read_stdin()
        if _bypassing(data):
            _FALLBACK = None
        HANDLERS[args.cmd](data)
    except Exception as exc:  # a hook must never take the session down with it
        print(f"jev {args.cmd}: {type(exc).__name__}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
