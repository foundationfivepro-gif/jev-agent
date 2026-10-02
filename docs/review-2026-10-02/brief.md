# Review brief: Jev model routing ("delegation-economics" skill)

Owner goals (2026-10-02), in priority order:
1. Never introduce an unnecessary human intervention that stops work. A route result, a
   hook, a doc line or a policy sentence that makes an agent pause and ask a person when it
   could safely proceed is a defect.
2. Make optimal use of Claude Sonnet 5.5 (claude-sonnet-5-5, $2/$10 per MTok). It is now
   highly capable: everyday coding, multi-file edits, agentic tool use, research. Opus 5.5
   ($4/$20) should be reserved for what Sonnet really falls short on.
3. Efficiency: fewest tokens and dollars for work that passes acceptance.
Fixed constraints: Fable (claude-fable-5-1) is explicit-only, never selected by the router
(owner decision, just merged). Explicit `model` on a spawn is always kept.

Files in scope (repo root = this directory):
- skills/delegation-economics/SKILL.md  (the skill)
- model_router.py                        (route_model, _fallback, estimate_costs)
- hooks.py: route_agent()               (PreToolUse hook on every Agent spawn)
- mcp_server.py jev_route_model, remote_server.py route tool docstrings
- CLAUDE.md (policy loaded in every session, must stay < 2000 chars)
- tests/test_all.py routing tests (run: python3 -m pytest tests/ -q)

Return: a numbered list of concrete findings, each with file:line, the problem, why it
matters against the goals above, and the exact change you recommend. Rank by impact.
Say explicitly where you think the current design is already right. No preamble.
