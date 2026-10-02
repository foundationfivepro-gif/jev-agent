#!/usr/bin/env bash
# Jev hooks for Claude Code cloud sessions (claude.ai/code) in this repository.
# Installed by jev-agent `hooks.py install-cloud`; do not edit here, reinstall instead.
#
# Cloud VMs do not read the user's ~/.claude settings, so the repository carries
# the hooks. On a local machine (CLAUDE_CODE_REMOTE unset) this exits at once:
# the user-scope install already runs them. In the cloud it fetches jev-agent at
# session start; if that is not reachable, subagents still get Sonnet, never the
# parent's model by inheritance.
set -u
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
JEV="${JEV_AGENT_DIR:-$HOME/.jev-agent}"
SUB="${1:-}"

if [ "$SUB" = "session" ] && [ ! -f "$JEV/hooks.py" ]; then
  git clone -q --depth 1 https://github.com/foundationfivepro-gif/jev-agent.git "$JEV" \
    >/dev/null 2>&1 || echo "jev: jev-agent not reachable; subagents default to sonnet" >&2
fi

floor() {  # stdin: the hook payload. Pins a spawn with no explicit model to sonnet.
  python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ti = dict(data.get("tool_input") or {})
if ti.get("model"):
    sys.exit(0)
ti["model"] = "sonnet"
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "jev: sonnet (Jev not available in this VM)",
    "updatedInput": ti}}))
'
}

if [ ! -f "$JEV/scripts/cloud.sh" ]; then
  [ "$SUB" = "route-agent" ] && floor
  exit 0
fi
if [ "$SUB" != "route-agent" ]; then
  exec bash "$JEV/scripts/cloud.sh" hook "$SUB"
fi
# Full router first; if it said nothing (missing deps, crash), the floor decides.
payload="$(cat)"
out="$(printf '%s' "$payload" | bash "$JEV/scripts/cloud.sh" hook route-agent 2>/dev/null)"
if [ -n "$out" ]; then printf '%s\n' "$out"; else printf '%s' "$payload" | floor; fi
exit 0
