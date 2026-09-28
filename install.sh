#!/usr/bin/env bash
# Install jev-agent for Claude Code.
#
#   skills      SKILL.md files -> ~/.claude/skills. Only their frontmatter loads
#               until a description matches, so they cost almost nothing idle.
#
#   hooks       hooks.py into ~/.claude/settings.json: the command gate, the
#               subagent router and return contract, the per-prompt note and the
#               outcome recorder run on every event. Also places the defaults
#               policy (CLAUDE.md) in ~/.claude/CLAUDE.md; the two go together.
#
#   MCP server  The decision tools, chiefly jev_select_context, which runs
#               before Claude reads files. Needs OPENROUTER_API_KEY (or TYPESAFE_API_KEY).
#
# Usage:
#   ./install.sh skills | hooks | mcp | all
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-all}"

install_skills() {
  local dir="$HOME/.claude/skills"
  mkdir -p "$dir"
  for skill in "$HERE"/skills/*/; do
    name="$(basename "$skill")"
    rm -rf "${dir:?}/$name"
    cp -R "$skill" "$dir/$name"
  done
  echo "  installed $(ls -1 "$HERE"/skills | wc -l | tr -d ' ') skills -> $dir"
  echo
  echo "  NOTE: these are the CLI/desktop copies. Skills saved to your Claude"
  echo "  account (via the app) are what sync to mobile — files on disk do not."
}

print_mcp() {
  local py; py="$(command -v python3 || echo python3)"
  cat <<EOF
  Requirements, once:
      $py -m pip install -r "$HERE/requirements.txt"

  Claude Code (desktop + CLI):
      claude mcp add jev --scope user -- $py "$HERE/mcp_server.py"

  The key comes from $HERE/.env (OPENROUTER_API_KEY; TYPESAFE_API_KEY or
  AI_GATEWAY_API_KEY as fallbacks), which the server reads at start.

  Verify:
      $py "$HERE/mcp_server.py"   # should sit waiting on stdio

  The server is read-only: it decides, it never edits files or runs commands.
EOF
}

case "$MODE" in
  skills) echo "Installing skills..."; install_skills ;;
  hooks)  echo "Installing hooks..."; python3 "$HERE/hooks.py" install ;;
  mcp)    echo "MCP registration:"; echo; print_mcp ;;
  all)
    echo "Installing skills..."; install_skills; echo
    echo "Installing hooks..."; python3 "$HERE/hooks.py" install; echo
    echo "MCP registration:"; echo; print_mcp
    ;;
  *) echo "usage: $0 [skills|hooks|mcp|all]" >&2; exit 1 ;;
esac
