# jev-agent defaults

A session-start note says `jev hooks active` when hooks run. They gate recognized
outbound Bash commands and route subagent spawns. Do not duplicate those calls.
Without that note, call the tools yourself; host permissions always apply.

## Call these yourself

- Before finding repository files: `jev_select_context`. Read only `include`, not
  `index`. Local reads require operator-approved workspace roots.
- Before sharing content: `jev_classify_data`. `secret` means stop; a classification
  never grants consent to transmit.
- Before external effects (send, post, delete, pay, deploy): `jev_check_action`.

## With `jev hooks active`

- Bash: irreversible deterministic rules deny. Model block/review, unavailable
  service and errors ask. Opaque interpreter/script execution asks without a model
  call. Model allow stays silent; it cannot override host permission.
- Agent spawns without `model` use `jev_route_model`. Explicit models stay intact.
  Every subagent gets a compact return contract with conclusions and file:line refs.

## Without hooks

- Before an outbound command: `jev_gate_command`. Never execute a policy block;
  block/review or unavailable outcomes require host approval.
- Before delegating: `jev_route_model`; `human` means do not delegate.

## Delegation

Delegate when much inspection produces a small result. Keep available parent
context inline. Fable remains escalation-only, never an uncertain fallback.

Only screened prompts and command metadata reach the configured Jev decision
route (OpenRouter by default). Source contents stay local by default. Detection
is defense in depth, not consent. Arbitrary programs still need the host sandbox.

## Verify

`python scripts/offline_tests.py` runs without credentials or external sockets.
Every tool must import a real module and be named in a skill.
