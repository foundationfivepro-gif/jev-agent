# jev-agent

Source for the `jev` MCP server, hooks (`hooks.py`) and `skills/`. `CLAUDE.md` here is the policy block `hooks.py install` places in `~/.claude/CLAUDE.md`; keep it under 2,000 chars.

Verify: `python3 -m pytest tests/ -q`. Every MCP tool must import from a real module and be named in a skill.
