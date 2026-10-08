# jev-agent defaults

Jev is an efficiency advisor, never a gatekeeper: it picks the cheapest sufficient model, the least context and the right skill. It never approves, blocks or asks permission for anything, so don't stop or ask the user because of Jev.

A session-start note `jev hooks active` means hooks route every subagent spawn (model plus a conclusion-only return contract); don't repeat that call. Without it (Cowork, claude.ai, no hooks), call `jev_route_model` before delegating, asking for the conclusion only.

- Before reading files to find something: `jev_select_context`; read `include`, `index` only when needed.
- Before sending file contents to a third party: `jev_classify_data`; never send a `secret`.

## Memory
Memory workspace: ops  (Agency Memory connector)
- Call `context` once at session start for this workspace (the task in one line); call it again only if the task changes.
- Before reading wikis, specs, transcripts or old threads to answer "what did we decide / why", call `recall` (k=3) in this workspace only.
- At the end, `remember` at most 3 durable lessons (1-3 sentences, dated, with the why). If one corrects an older memory, save the fix, then `forget` the old id.
- Never store secrets or lead, customer or downline personal data.
- If the connector isn't available in this session, say so once and carry on.
