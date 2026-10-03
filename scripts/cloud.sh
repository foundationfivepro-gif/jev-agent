#!/usr/bin/env bash
# Glue for Claude Code cloud sessions (claude.ai/code, `claude --cloud`).
#
# On your own machine the user-scope install (./install.sh) already runs the
# hooks and the MCP server, so the project-level entries in .claude/settings.json
# and .mcp.json must not run them a second time. Cloud VMs set
# CLAUDE_CODE_REMOTE=true; everywhere else this script is a no-op.
#
#   cloud.sh hook <sub>   run `hooks.py <sub>` (cloud only; `session` also installs deps)
#   cloud.sh mcp          start mcp_server.py on stdio, installing deps first in the cloud
#
# The key: add OPENROUTER_API_KEY (or TYPESAFE_API_KEY) to the cloud environment. See README, "Cloud sessions".
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE=false; [ "${CLAUDE_CODE_REMOTE:-}" = "true" ] && REMOTE=true
# scripts/cloud-user.sh already runs both at user scope; don't run them twice.
if [ -d "$HOME/.jev-agent" ] && [ "$ROOT" != "$HOME/.jev-agent" ]; then REMOTE=false; fi

have_deps() { python3 -c "import typesafe_sdk, mcp, ast_grep_py" 2>/dev/null; }

# stdout belongs to the hook or MCP protocol, so pip talks to stderr only. The
# lock stops the SessionStart hook and the MCP server installing at once.
deps() {
  have_deps && return 0
  (
    flock 9
    have_deps && exit 0
    python3 -m pip install -q -r "$ROOT/requirements.txt" 1>&2 \
      || python3 -m pip install -q --break-system-packages -r "$ROOT/requirements.txt" 1>&2
  ) 9>/tmp/jev-deps.lock
}

case "${1:-}" in
  hook)
    $REMOTE || exit 0
    [ "${2:-}" = "session" ] && deps
    exec python3 "$ROOT/hooks.py" "${2:-}"
    ;;
  mcp)
    $REMOTE && deps
    exec python3 "$ROOT/mcp_server.py"
    ;;
esac
exit 0
