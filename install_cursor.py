#!/usr/bin/env python3
"""
Install jev-agent into Cursor.

Cursor differs from Claude Code and Codex in two ways that matter:

  * It does not read SKILL.md. Its equivalent is `.cursor/rules/*.mdc` —
    markdown with different frontmatter — so the skills are converted.
  * Rules are PROJECT-scoped and version-controlled, not global. There is no
    `~/.agents/skills` equivalent, so rules install per repository and you pass
    the repo path. (MCP config is global and installs once.)

The mcp.json write is a MERGE, not an overwrite: your existing servers are kept
and a timestamped backup is written before anything changes.

    python3 install_cursor.py --repo ~/code/myproject
    python3 install_cursor.py --repo ~/code/myproject --dry-run
    python3 install_cursor.py --mcp-only
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS = HERE / "skills"

# Cursor rule modes: alwaysApply true, globs-attached, agent-selected
# (description only), or manual. Agent-selected is the closest analogue to how a
# skill behaves — the model pulls it in when the description matches — so that is
# the default. Rules that are cheap and broadly useful get globs so they attach
# automatically when relevant files are in context.
GLOBS: dict[str, list[str]] = {
    "context-tiering": ["**/*.py", "**/*.ts", "**/*.tsx", "**/*.js"],
    "jev-evaluation": ["**/*jev*", "**/*evaluate*"],
}


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Minimal YAML frontmatter reader — SKILL.md only ever has name/description."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw, body = text[3:end], text[end + 4 :]
    meta: dict[str, str] = {}
    key = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, val = line.partition(":")
            key = key.strip()
            meta[key] = val.strip()
        elif key:                       # folded continuation line
            meta[key] += " " + line.strip()
    return meta, body.lstrip("\n")


def yaml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_mdc(skill_dir: Path) -> tuple[str, str]:
    meta, body = split_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    name = meta.get("name", skill_dir.name)
    description = meta.get("description", "")
    globs = GLOBS.get(name)

    lines = ["---", f"description: {yaml_quote(description)}"]
    if globs:
        lines.append("globs: " + json.dumps(globs))
    lines.append("alwaysApply: false")
    lines.append("---")
    lines.append("")
    return name, "\n".join(lines) + "\n" + body


def install_rules(repo: Path, *, dry_run: bool) -> list[Path]:
    target = repo / ".cursor" / "rules"
    written: list[Path] = []
    for skill_dir in sorted(p for p in SKILLS.iterdir() if (p / "SKILL.md").is_file()):
        name, content = to_mdc(skill_dir)
        dest = target / f"{name}.mdc"
        written.append(dest)
        if dry_run:
            continue
        target.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    return written


def install_mcp(*, dry_run: bool, key: str | None) -> tuple[Path, dict, Path | None]:
    cfg_path = Path.home() / ".cursor" / "mcp.json"
    existing: dict = {}
    if cfg_path.is_file():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"{cfg_path} is not valid JSON ({exc}). Fix or move it before installing — "
                "refusing to overwrite a file I cannot parse."
            )

    servers = existing.setdefault("mcpServers", {})
    entry = {
        "command": sys.executable or "python3",
        "args": [str(HERE / "mcp_server.py")],
        "env": {"AI_GATEWAY_API_KEY": key or "${AI_GATEWAY_API_KEY}"},
    }
    previous = servers.get("jev")
    servers["jev"] = entry

    backup = None
    if not dry_run:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        if cfg_path.is_file():
            backup = cfg_path.with_suffix(
                f".json.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            )
            shutil.copy2(cfg_path, backup)
        cfg_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return cfg_path, {"entry": entry, "replaced": previous, "kept": [k for k in servers if k != "jev"]}, backup


def main() -> None:
    ap = argparse.ArgumentParser(description="Install jev-agent into Cursor.")
    ap.add_argument("--repo", type=Path, help="Repository to install .cursor/rules into.")
    ap.add_argument("--mcp-only", action="store_true", help="Skip rules; configure MCP only.")
    ap.add_argument("--rules-only", action="store_true", help="Skip MCP; install rules only.")
    ap.add_argument("--dry-run", action="store_true", help="Show what would change.")
    ap.add_argument("--key", default=os.environ.get("AI_GATEWAY_API_KEY"),
                    help="AI_GATEWAY_API_KEY to embed. Omitted -> ${AI_GATEWAY_API_KEY}.")
    args = ap.parse_args()

    if not args.mcp_only:
        if not args.repo:
            ap.error("--repo is required for rules (Cursor rules are project-scoped). "
                     "Use --mcp-only to configure just the server.")
        repo = args.repo.expanduser().resolve()
        if not repo.is_dir():
            ap.error(f"not a directory: {repo}")
        written = install_rules(repo, dry_run=args.dry_run)
        verb = "would write" if args.dry_run else "wrote"
        print(f"rules — {verb} {len(written)} .mdc files to {repo / '.cursor' / 'rules'}")
        for p in written:
            print(f"    {p.name}")
        print("    mode: agent-selected (alwaysApply false) — Cursor pulls one in when its")
        print("    description matches. Two carry globs and auto-attach on source files.")
        print("    These are project-scoped: re-run per repository.")

    if not args.rules_only:
        print()
        cfg, info, backup = install_mcp(dry_run=args.dry_run, key=args.key)
        verb = "would update" if args.dry_run else "updated"
        print(f"mcp   — {verb} {cfg}")
        if info["kept"]:
            print(f"    kept existing servers: {', '.join(info['kept'])}")
        if info["replaced"]:
            print("    replaced a previous 'jev' entry")
        if backup:
            print(f"    backup: {backup.name}")
        if not args.key:
            print("    env uses ${AI_GATEWAY_API_KEY} — Cursor does not expand shell vars in")
            print("    mcp.json, so either pass --key or switch that line to an envFile path.")
        print("    Restart Cursor, then check Settings → MCP for the 'jev' server.")


if __name__ == "__main__":
    main()
