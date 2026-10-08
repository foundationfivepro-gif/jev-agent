#!/bin/sh
# Agency Memory start-of-session nudge for cloud sessions and routines (CLAUDE_CODE_REMOTE=true).
# Local Mac sessions are covered by the global hook (~/.claude/hooks/agency-memory-context.sh), so this stays silent there.
# No network, no credentials. Disable with AGENCY_MEMORY_DISABLE=1.
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
[ "${AGENCY_MEMORY_DISABLE:-}" = "1" ] && exit 0
ws=$(grep -m1 -oE '^Memory workspace: *(fs|tri|bb|wbm|ops|jks)' "${CLAUDE_PROJECT_DIR:-.}/CLAUDE.md" "${CLAUDE_PROJECT_DIR:-.}/AGENTS.md" 2>/dev/null | head -1 | awk '{print $NF}')
echo "Agency Memory (cloud session): before other work, call the Agency Memory connector's \`context\` tool once for workspace ${ws:-<see the Memory section of CLAUDE.md>} with the task in one line. Call \`recall\` (k=3) before re-researching a client fact or answering a policy or decision question. End substantive tasks with one work-log episode (kind episode, tag work-log). If the connector isn't connected, say so once and carry on."
