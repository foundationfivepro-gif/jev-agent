#!/usr/bin/env bash
# Install jev-agent for Claude Code.
#
#   skills      skills/* linked into ~/.claude/skills and ~/.agents/skills;
#               REPO_ONLY skills load only inside this checkout.
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

# Repo-only skills load from .claude/skills inside this checkout; the rest are
# linked (not copied) into the user skill dirs so edits here apply at once.
REPO_ONLY="jev-evaluation skill-routing"

install_skills() {
  local name dir n=0
  for dir in "$HOME/.claude/skills" "$HOME/.agents/skills"; do
    mkdir -p "$dir"
    for skill in "$HERE"/skills/*/; do
      name="$(basename "$skill")"
      rm -rf "${dir:?}/$name"
      case " $REPO_ONLY " in *" $name "*) continue ;; esac
      ln -s "${skill%/}" "$dir/$name"
    done
  done
  mkdir -p "$HERE/.claude/skills"
  for name in $REPO_ONLY; do ln -sfn "../../skills/$name" "$HERE/.claude/skills/$name"; done
  n=$(ls -1 "$HERE"/skills | wc -l | tr -d ' ')
  echo "  linked $n skills -> ~/.claude/skills and ~/.agents/skills ($REPO_ONLY: this repo only)"
  echo
  echo "  NOTE: skills saved to your Claude account (via the app) sync separately;"
  echo "  delete account copies of these skills or they are listed twice."
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
