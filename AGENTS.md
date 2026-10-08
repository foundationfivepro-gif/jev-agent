# jev-agent

Source for the `jev` MCP server, hooks (`hooks.py`) and `skills/`. `CLAUDE.md` here is the policy block `hooks.py install` places in `~/.claude/CLAUDE.md`; keep it under 2,000 chars.

Verify: `python3 -m pytest tests/ -q`. Every MCP tool must import from a real module and be named in a skill.

## Memory
Memory workspace: ops  (Agency Memory connector)
- Call `context` once at session start for this workspace (the task in one line); call it again only if the task changes. If an "Agency Memory context" block was already injected at session start (Mac hook), that counts: don't call it again.
- Before re-researching any client fact (settings, prices, deploy targets, accounts, past incidents) and before answering a policy or "what did we decide / why" question, call `recall` (k=3) in this workspace first. Read wikis, specs, transcripts or old threads only for what memory doesn't answer. Don't repeat near-identical recalls.
- At the end, `remember` at most 3 durable lessons (1-3 sentences, dated, with the why). If one corrects an older memory, save the fix, then `forget` the old id.
- Work log: at the end of every substantive task (code change, PR, merge, deploy, data/config change, investigation with findings), `remember` one episode (kind "episode", tag "work-log") in this workspace: what was done, by which agent (claude-code / claude-cloud / claude-routine / codex), repo + PR number + merge SHA, deploy/version id, and anything left open.
- Never store secrets or lead, customer or downline personal data.
- If the connector isn't available in this session, say so once and carry on.
