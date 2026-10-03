#!/usr/bin/env bash
# User-scope install for Claude Code cloud environments, run from the
# environment's setup script.
#
# Project threads start in a parent folder (e.g. /home/user) with each repo
# cloned beneath it, so a repo's own .claude/settings.json and .mcp.json are
# never read. This puts Jev where every session looks instead:
#
#   ~/.jev-agent            a copy of this repo, so hook paths stay stable
#   ~/.claude/settings.json the hooks (via `hooks.py install`), plus ~/.claude/CLAUDE.md
#   ~/.claude/skills        the skills
#   ~/.claude.json          the `jev` MCP server at user scope
#
# Setup script line:
#   bash "$(ls -d /home/user/jev-agent ~/jev-agent 2>/dev/null | head -1)/scripts/cloud-user.sh"
#
# Safe to run more than once. Once ~/.jev-agent exists, scripts/cloud.sh stands
# down so a session started inside a repo does not run the hooks twice.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$HOME/.jev-agent"

if [ "$SRC" != "$DEST" ]; then
  mkdir -p "$DEST"
  tar -C "$SRC" --exclude=.git --exclude=__pycache__ -cf - . | tar -C "$DEST" -xf -
fi

python3 -m pip install -q -r "$DEST/requirements.txt" \
  || python3 -m pip install -q --break-system-packages -r "$DEST/requirements.txt"

bash "$DEST/install.sh" skills
python3 "$DEST/hooks.py" install

# User-scope MCP servers live under the top-level "mcpServers" key of ~/.claude.json.
python3 - "$DEST" <<'EOF'
import json, sys
from pathlib import Path

dest = sys.argv[1]
path = Path.home() / ".claude.json"
data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
data.setdefault("mcpServers", {})["jev"] = {
    "type": "stdio", "command": "python3", "args": [f"{dest}/mcp_server.py"], "env": {},
}
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(f"mcp      — wrote jev to {path} (user scope)")
EOF
