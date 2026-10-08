#!/bin/sh
# Agency Memory start-of-session context for cloud sessions and routines (CLAUDE_CODE_REMOTE=true).
# Local Mac sessions are covered by the global hook (~/.claude/hooks/agency-memory-context.sh), so this stays silent there.
# v2 (2026-10-08): if the cloud environment sets AGENCY_MEMORY_CLOUD_TOKEN (read-only token; the gateway stores only its
# SHA-256) and the network allows agency-memory.foundationfivepro.workers.dev, inject the workspace `context` directly.
# Otherwise (or on any failure) print the "call context first" directive. Never blocks: curl --max-time 4, exit 0.
# Disable with AGENCY_MEMORY_DISABLE=1.
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
[ "${AGENCY_MEMORY_DISABLE:-}" = "1" ] && exit 0
dir="${CLAUDE_PROJECT_DIR:-.}"
ws=$(grep -m1 -oE '^Memory workspace: *(fs|tri|bb|wbm|ops|jks)' "$dir/CLAUDE.md" "$dir/AGENTS.md" 2>/dev/null | head -1 | awk '{print $NF}')
rule="RULE: before answering any client fact, policy, link, price or \"what did we decide\" question, and before any deploy, call \`recall\` (k=3) in this workspace unless the exact fact is shown here. End substantive tasks with one work-log episode (kind episode, tag work-log)."
if [ -n "${AGENCY_MEMORY_CLOUD_TOKEN:-}" ] && [ -n "$ws" ] && command -v curl >/dev/null 2>&1; then
  tmp=$(mktemp 2>/dev/null || echo "/tmp/amctx.$$")
  code=$(curl -sS --max-time 4 -o "$tmp" -w '%{http_code}' -X POST "${AGENCY_MEMORY_URL:-https://agency-memory.foundationfivepro.workers.dev}/svc/context" \
    -H "Authorization: Bearer $AGENCY_MEMORY_CLOUD_TOKEN" -H 'Content-Type: application/json' -A 'agency-memory-cloud-hook/2' \
    --data-binary "{\"workspace\":\"$ws\",\"task\":\"Cloud session start in $(basename "$dir"): load prior decisions, lessons and open items for this repo.\"}" 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then
    echo "Agency Memory context for workspace $ws (loaded at session start by the cloud hook; this counts as the session's \`context\` call, so don't repeat it). These are only the top memories, not everything memory knows. $rule"
    tail -n +2 "$tmp" | head -c 6000
    rm -f "$tmp"; exit 0
  fi
  rm -f "$tmp"
fi
echo "Agency Memory (cloud session): before other work, call the Agency Memory connector's \`context\` tool once for workspace ${ws:-<see the Memory section of CLAUDE.md>} with the task in one line. $rule If the connector isn't connected, say so once and carry on."
