# Round 2 adjudication (Claude)
Accepted and implemented: 1, 2, 3 (shared `eligible_models` + `floor_model` in model_router,
used by route_model, the hook and both MCP fallbacks; nothing excluded is ever restored; no
eligible model -> hook leaves only the contract, MCP raises "do the work inline; do not stop"),
4 partly (JEV_CATALOG=codex is not implemented: AGENTS.md now says the tool returns Claude tier
names and Codex maps haiku/sonnet/opus -> Luna/Terra/Sol; implementing a Codex catalog needs
real Terra/Sol prices, out of scope), 5 as wording (labelled a heuristic pending calibration,
assumptions stated; rule kept because it tracks the price list), 7, 8.
Out of scope, reported to the owner instead: 6 (the shell-command gate's `review` semantics
are a security boundary in owner-approved policy; this task is model routing).
Finding 7 of round 1 stays rejected: docs list updatedInput and permissionDecision separately
and do not state their interaction; `allow` is the only choice that cannot add a prompt.
