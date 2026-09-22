# jev-agent defaults

The `jev` MCP server is registered in this harness. Its tools are the default, not an
option, for the top-level session and for every subagent spawned from it.

## Always

- **Before reading files to find something:** `jev_select_context`. Read only `include`;
  never read `index`.
- **Before any shell command:** `jev_gate_command`. Never run a `review` or `block` result.
- **Before sending file contents to any third party:** `jev_classify_data`. `secret` means stop.
- **Before spawning a subagent:** `jev_route_model`. Pass `selected` verbatim as the
  subagent's model (Claude Code: the Agent tool's `model` parameter). `human` means ask
  instead of delegating.

## Routing rules

These come from `model_router.py` and `skills/delegation-economics/SKILL.md`.

- Delegate on **compression ratio, not difficulty**. A subagent that reads a lot and returns
  a few sentences pays for itself; one whose full output you must re-read does not — do that
  work inline instead.
- Every subagent prompt says what to return: the conclusion, not the evidence.
- `jev_route_model` already fails toward capability: below 0.75 confidence it returns the
  strongest ordinary tier (Opus in the default catalog). Do not second-guess it downward.
- **Fable (`claude-fable-5-1`) is the most expensive tier — $10/$50 per MTok, twice Opus.**
  It is escalation-only: never the default for delegated work, never the fallback for an
  uncertain route, only what the router returns when it judges Opus insufficient.
- Do not delegate what is already loaded in context. Warm context is nearly free.

## In Claude Code, hooks enforce this

`hooks.py` is installed into `~/.claude/settings.json`: every prompt is scored by Jev,
every Bash command passes `jev_gate_command`, and every subagent without an explicit model
gets one from `jev_route_model`. The tools above are still worth calling directly — the
hooks are the floor, not the ceiling.

That means prompt text and command lines are sent to Jev through the gateway in every
session. Anything credential-shaped is held back locally and becomes `ask` instead.

## Verify

`python -m pytest tests/ -q` — most tests need no key. Two guards enforce that every MCP
tool imports from a real module and is named in a skill; a new tool must satisfy both.
