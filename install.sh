#!/usr/bin/env bash
# Install jev-agent for Claude Code and/or OpenAI Codex.
#
# Two separate things get installed, and they behave differently:
#
#   skills      Markdown files. Same SKILL.md format in both clients, so the same
#               files serve Claude Code (~/.claude/skills) and Codex
#               (~/.agents/skills). They load when their description matches the
#               task. No key, no process, no cost until they load.
#
#   MCP server  A local stdio process exposing the decision tools. Needs
#               AI_GATEWAY_API_KEY and a Python environment. This is the part
#               that produces the token saving, because jev_select_context runs
#               before the agent reads files.
#
# Usage:
#   ./install.sh skills           # skills -> Claude Code + Codex
#   ./install.sh mcp              # print MCP registration for Claude Code + Codex
#   ./install.sh cursor REPO      # Cursor: convert skills to .mdc + merge mcp.json
#   ./install.sh all              # skills + mcp (Cursor is separate; see above)
#
# Cursor is handled by install_cursor.py because it differs twice: it does not
# read SKILL.md (its rules are .cursor/rules/*.mdc), and those rules are
# PROJECT-scoped rather than global, so they install per repository.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-all}"

install_skills() {
  local installed=0
  for target in "$HOME/.claude/skills:Claude Code" "$HOME/.agents/skills:Codex"; do
    dir="${target%%:*}"; label="${target##*:}"
    mkdir -p "$dir"
    for skill in "$HERE"/skills/*/; do
      name="$(basename "$skill")"
      rm -rf "${dir:?}/$name"
      cp -R "$skill" "$dir/$name"
    done
    echo "  installed $(ls -1 "$HERE"/skills | wc -l | tr -d ' ') skills -> $dir  ($label)"
    installed=1
  done
  [ "$installed" = 1 ] || echo "  no skills installed"
  echo
  echo "  Claude Code picks these up in any repo. Codex finds them at USER scope;"
  echo "  invoke explicitly with \$jev-evaluation or let the description match."
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
      claude mcp add jev \\
        --env AI_GATEWAY_API_KEY=\$AI_GATEWAY_API_KEY \\
        -- $py "$HERE/mcp_server.py"

  Codex — append to ~/.codex/config.toml:

      [mcp_servers.jev]
      command = "$py"
      args = ["$HERE/mcp_server.py"]
      env = { AI_GATEWAY_API_KEY = "vck_..." }
      startup_timeout_sec = 30

  or:
      codex mcp add jev -- $py "$HERE/mcp_server.py"
      # then add the env key to the generated table

  Verify:
      AI_GATEWAY_API_KEY=... $py "$HERE/mcp_server.py"   # should sit waiting on stdio

  The server is read-only: it decides, it never edits files or runs commands.
EOF
}

case "$MODE" in
  cursor)
    shift || true
    repo="${1:-}"
    if [ -z "$repo" ]; then
      echo "usage: $0 cursor /path/to/repo   (Cursor rules are project-scoped)" >&2
      echo "       $0 cursor --mcp-only      (server only, no rules)" >&2
      exit 1
    fi
    if [ "$repo" = "--mcp-only" ]; then
      exec python3 "$HERE/install_cursor.py" --mcp-only
    fi
    exec python3 "$HERE/install_cursor.py" --repo "$repo"
    ;;
  skills) echo "Installing skills..."; install_skills ;;
  mcp)    echo "MCP registration:"; echo; print_mcp ;;
  all)    echo "Installing skills..."; install_skills; echo; echo "MCP registration:"; echo; print_mcp ;;
  *) echo "usage: $0 [skills|mcp|cursor REPO|all]" >&2; exit 1 ;;
esac
