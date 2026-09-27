# jev-agent defaults

When the hooks are running, a session-start note says `jev hooks active`. They then run
Jev on every Bash command and every subagent spawn; do not repeat those calls by hand,
since each is a tool round-trip in your context plus a second Jev call. With no such note
(Cowork, claude.ai, a machine without the hooks), make those calls yourself.

## Call these yourself

- **Before reading files to find something:** `jev_select_context`. Read only `include`;
  never read `index`.
- **Before sending file contents to a third party:** `jev_classify_data`. `secret` means stop.
- **Before an action with external effect** (send, post, delete, pay, deploy):
  `jev_check_action`.

## With `jev hooks active`, the hooks do these

- **Bash** is gated by `jev_gate_command`. The hook's `deny` or `ask` is the decision.
- **Agent spawns** without a `model` are routed by `jev_route_model`, and every subagent
  prompt gets a return contract (conclusion only, file:line references). Set `model`
  yourself only when you know better; the hook keeps it.

## Without it, do them yourself

- Before a shell command: `jev_gate_command`. Never run a `policy` block; a model `review`/`block` is advice — the user's permission mode decides.
- Before delegating: `jev_route_model`; use `selected` as the model (`human`: don't
  delegate), and end the subagent prompt by asking for the conclusion only.

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
