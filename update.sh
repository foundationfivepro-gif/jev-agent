#!/usr/bin/env bash
# Bring this computer's jev-agent install up to date, or install it for the
# first time. Safe to run repeatedly.
#
#   1. git pull (fast-forward only; local edits are never overwritten)
#   2. Python requirements, only if the interpreter cannot import them
#   3. skills, hooks and policy (./install.sh skills + hooks.py install)
#   4. /jev-update, so this runs from inside Claude Code next time
#   5. the MCP server, registered at user scope if it is not registered yet
#   6. a check that the hooks answer
#
# Uses the interpreter the installed hooks already run under, so an update
# never moves them to a Python that lacks the requirements.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"

say()  { printf '\n== %s\n' "$*"; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

PY="$(python3 - "$SETTINGS" "$HERE/hooks.py" 2>/dev/null <<'EOF' || true
import json, sys
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    sys.exit()
for groups in (s.get("hooks") or {}).values():
    for g in groups:
        for h in g.get("hooks", []):
            if sys.argv[2] in (h.get("args") or []):
                print(h.get("command", "")); sys.exit()
EOF
)"
PY="${JEV_PYTHON:-${PY:-$(command -v python3 || true)}}"
[ -x "$PY" ] || fail "no python3 found"

if [ "${JEV_UPDATE_PULLED:-}" != 1 ]; then
  if [ -n "$(git -C "$HERE" status --porcelain --untracked-files=no)" ]; then
    fail "local changes in $HERE; commit or stash them, then re-run"
  fi
  git -C "$HERE" pull --ff-only --quiet
  # The pull may have rewritten this very file while bash is reading it.
  # Continue in the freshly pulled copy instead.
  JEV_UPDATE_PULLED=1 exec bash "$HERE/update.sh" "$@"
fi
say "1/6 Updated $HERE"
git -C "$HERE" log -1 --format='   now at %h %s'

say "2/6 Python requirements ($PY)"
if "$PY" -c "import typesafe_sdk, mcp, pydantic" 2>/dev/null; then
  echo "   already installed"
else
  "$PY" -m pip install --quiet -r "$HERE/requirements.txt" \
    || fail "pip could not install into $PY. Use a virtualenv, e.g.
   python3 -m venv ~/.jev-venv && ~/.jev-venv/bin/pip install -r $HERE/requirements.txt
   then re-run with: JEV_PYTHON=~/.jev-venv/bin/python $0"
fi

say "3/6 Skills, hooks and policy"
bash "$HERE/install.sh" skills | sed 's/^/   /'
"$PY" "$HERE/hooks.py" install | sed 's/^/   /'

say "4/6 /jev-update command"
# User-invoked only (disable-model-invocation), so its description never
# loads into Claude's context on its own.
mkdir -p "$HOME/.claude/skills/jev-update"
cat > "$HOME/.claude/skills/jev-update/SKILL.md" <<EOF
---
name: jev-update
description: Update this computer's jev-agent install (pull, reinstall hooks, skills, policy, MCP).
disable-model-invocation: true
---

Run \`bash "$HERE/update.sh"\` and report its final summary in two lines. If it fails,
show the error line and the fix it suggests.
EOF
echo "   wrote ~/.claude/skills/jev-update — type /jev-update in Claude Code next time"

say "5/6 MCP server"
# Same order as core.TRANSPORTS: OpenRouter, TypeSafe, then the legacy gateway.
KEY=""; KEY_VAR=""
for var in OPENROUTER_API_KEY TYPESAFE_API_KEY AI_GATEWAY_API_KEY; do
  val="${!var:-}"
  if [ -z "$val" ] && [ -f "$HERE/.env" ]; then
    val="$(sed -n "s/^[[:space:]]*$var[[:space:]]*=[[:space:]]*//p" "$HERE/.env" | head -n1 | tr -d "\"'")"
  fi
  if [ -n "$val" ]; then KEY="$val"; KEY_VAR="$var"; break; fi
done
if ! command -v claude >/dev/null 2>&1; then
  echo "   claude CLI not on PATH; skipped. Register later with: bash $HERE/install.sh mcp"
elif claude mcp get jev >/dev/null 2>&1; then
  echo "   jev already registered (to switch keys: claude mcp remove jev --scope user, then re-run)"
elif [ -z "$KEY" ]; then
  echo "   no OPENROUTER_API_KEY (or TYPESAFE_API_KEY) in $HERE/.env; skipped. Add it and re-run."
else
  claude mcp add jev --scope user --env "$KEY_VAR=$KEY" -- "$PY" "$HERE/mcp_server.py" >/dev/null
  echo "   registered jev (user scope, $KEY_VAR)"
fi

say "6/6 Check"
NOTE="$(echo '{}' | "$PY" "$HERE/hooks.py" session)"
case "$NOTE" in
  *"jev hooks active"*) echo "   hooks answer: $(printf '%s' "$NOTE" | sed 's/.*"additionalContext": "\([^"]*\)".*/\1/')" ;;
  *) fail "the session hook did not answer; run: $PY $HERE/hooks.py session </dev/null" ;;
esac
[ -n "$KEY" ] || echo "   note: no key found, so only the local parts run (hard block, return contract)."

echo
echo "Done. Restart open Claude Code sessions to load the new hooks."
