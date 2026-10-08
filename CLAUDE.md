# jev-agent defaults

Jev is an efficiency advisor, never a gatekeeper: it picks the cheapest sufficient model, the least context and the right skill. It never approves, blocks or asks permission for anything, so don't stop or ask the user because of Jev.

A session-start note `jev hooks active` means hooks route every subagent spawn (model plus a conclusion-only return contract); don't repeat that call. Without it (Cowork, claude.ai, no hooks), call `jev_route_model` before delegating, asking for the conclusion only.

- Before reading files to find something: `jev_select_context`; read `include`, `index` only when needed.
- Before sending file contents to a third party: `jev_classify_data`; never send a `secret`.
