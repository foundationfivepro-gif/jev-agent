# jev-agent defaults

This policy is installed together with hooks that already run Jev on every prompt, every
Bash command and every subagent spawn. Do not repeat those calls by hand: each one is a
tool round-trip in your context plus a second Jev call for the same decision.

## Call these yourself

- **Before reading files to find something:** `jev_select_context`. Read only `include`;
  never read `index`.
- **Before sending file contents to a third party:** `jev_classify_data`. `secret` means stop.
- **Before an action with external effect** (send, post, delete, pay, deploy):
  `jev_check_action`.

## Hooks do these; don't

- **Bash** is gated by `jev_gate_command`. The hook's `deny` or `ask` is the decision.
- **Agent spawns** without a `model` are routed by `jev_route_model`, and every subagent
  prompt gets a return contract (conclusion only, file:line references). Set `model`
  yourself only when you know better; the hook keeps it.
- Call either tool directly only where no hook runs (the claude.ai remote connector).

## Delegating

- Delegate on **compression ratio, not difficulty**: a subagent that reads a lot and
  returns a few sentences pays; one whose output you must re-read does not. Do that inline.
- Do not delegate what is already in your context.
- Fable is escalation-only. Do not pick it, and do not second-guess the router downward.

Prompt text and command lines go to Jev through the gateway; anything credential-shaped
is held back locally and becomes `ask`.

## Verify

`python -m pytest tests/ -q`. Every MCP tool must import from a real module and be named
in a skill.
