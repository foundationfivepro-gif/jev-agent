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
#   plugin      TypeSafe's official skill (github.com/typesafe-ai/skills) at user
#               scope, so it is present in every session, not only in this repo
#               (whose .claude/settings.json enables it for the project). It is
#               the design half: primitives, confidence, live docs and cookbooks.
#               The jev-* skills here are the runtime half: this repo's tools,
#               transport and thresholds.
#
# Usage:
#   ./install.sh skills | hooks | mcp | plugin | all
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

install_plugin() {
  # Both commands are idempotent: an added marketplace and an installed plugin
  # each report so and exit 0. `update` picks up new skill revisions, which the
  # TypeSafe docs ask for (a stale skill invents request fields).
  if ! command -v claude >/dev/null 2>&1; then
    echo "  claude CLI not on PATH; skipped. Later:"
    echo "      claude plugin marketplace add typesafe-ai/skills"
    echo "      claude plugin install typesafe@typesafe-ai"
    return 0
  fi
  claude plugin marketplace add typesafe-ai/skills >/dev/null 2>&1 \
    || echo "  could not add the typesafe-ai marketplace (offline?); skipped"
  if claude plugin install typesafe@typesafe-ai --scope user >/dev/null 2>&1; then
    claude plugin update typesafe@typesafe-ai >/dev/null 2>&1 || true
    echo "  typesafe@typesafe-ai installed at user scope (invoke: /typesafe:typesafe-ai)"
  else
    echo "  could not install typesafe@typesafe-ai; run: claude plugin install typesafe@typesafe-ai"
  fi
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
  plugin) echo "Installing the TypeSafe plugin..."; install_plugin ;;
  all)
    echo "Installing skills..."; install_skills; echo
    echo "Installing the TypeSafe plugin..."; install_plugin; echo
    echo "Installing hooks..."; python3 "$HERE/hooks.py" install; echo
    echo "MCP registration:"; echo; print_mcp
    ;;
  *) echo "usage: $0 [skills|hooks|mcp|plugin|all]" >&2; exit 1 ;;
esac
