# jev-agent defaults

The `jev` MCP server is registered in this harness. Its tools are the default, not an
option, for the top-level session and for every subagent spawned from it.

## Always

- **Before reading files to find something:** `jev_select_context`. Read only `include`;
  never read `index`.
- **Before any shell command:** `jev_gate_command`. Never run a `review` or `block` result.
- **Before sending file contents to any third party:** `jev_classify_data`. `secret` means stop.
- **Before spawning a subagent:** `jev_route_model`. Pass `selected` verbatim as the
  subagent's model — Claude Code: the Agent tool's `model` parameter; Codex: the spawn's
  explicit model, which overrides `default_subagent_model`. `human` means ask instead of
  delegating. Claude Code applies this by hook; in Codex there is no spawn hook, so the
  call is yours to make every time.

## Routing rules

These come from `model_router.py` and `skills/delegation-economics/SKILL.md`.

- Delegate on **compression ratio, not difficulty**. A subagent that reads a lot and returns
  a few sentences pays for itself; one whose full output you must re-read does not — do that
  work inline instead.
- Every subagent prompt says what to return: the conclusion, not the evidence.
- `jev_route_model` already fails toward capability: below 0.75 confidence it returns the
  strongest ordinary tier (Opus in the default catalog). The exception is a mechanical task
  (complexity under 0.5), where a cheap tier is accepted from 0.5 — a Haiku retry on a
  one-line edit is nearly free. Do not second-guess it downward.
- **The top tier is escalation-only.** Fable (`claude-fable-5-1`, $10/$50 per MTok, twice
  Opus) and Astra (`gpt-6-astra`, $10/$50, fifty times Luna) are never the default for
  delegated work and never the fallback for an uncertain route. The router returns them
  when it judges a task frontier complexity — long-horizon architecture, system design,
  complex multi-page web design — where a wrong structural decision is expensive to unwind.
- The catalog follows the harness: Claude Code routes among haiku / sonnet / opus / fable,
  Codex among gpt-5.6-luna / -terra / -sol / gpt-6-astra (`JEV_CATALOG=codex` on its server).
- Do not delegate what is already loaded in context. Warm context is nearly free.

## Hooks enforce most of this

`hooks.py` is installed into `~/.claude/settings.json` and `~/.codex/hooks.json`: every
prompt is scored by Jev and every Bash command passes `jev_gate_command`. In Claude Code
every subagent without an explicit model also gets one from `jev_route_model`; Codex has
no spawn hook, so that step is on the agent. The tools above are still worth calling
directly — the hooks are the floor, not the ceiling.

That means prompt text and command lines are sent to Jev through the gateway in every
session. Anything credential-shaped is held back locally and becomes `ask` instead.

## Verify

`python -m pytest tests/ -q` — most tests need no key. Two guards enforce that every MCP
tool imports from a real module and is named in a skill; a new tool must satisfy both.
