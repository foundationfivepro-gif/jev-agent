#!/usr/bin/env python3
"""Prepare an additive, disabled Codex config candidate. Never install or log secrets."""
import argparse
import json
import os
from pathlib import Path
import sys
import tomllib


def prepare(existing: str, *, python: str, launcher: str, binding: str) -> str:
    parsed = tomllib.loads(existing)
    if 'jev_bound' in parsed.get('mcp_servers', {}):
        raise ValueError('jev_bound_already_configured')
    for value in (python, launcher, binding):
        if not Path(value).is_absolute() or any(ord(c) < 32 for c in value):
            raise ValueError('absolute_paths_required')
    # Appending preserves every existing setting/comment byte-for-byte.
    result = existing + '\n\n# Prepared JEV binding; enable only after host integration review.\n'
    result += '[mcp_servers.jev_bound]\n'
    result += 'command = ' + json.dumps(python) + '\n'
    result += 'args = ' + json.dumps([launcher, '--binding', binding]) + '\n'
    result += 'enabled = false\nstartup_timeout_sec = 20\ntool_timeout_sec = 40\n'
    result += 'enabled_tools = ["jev_recommend_host_route"]\n'
    result += 'env_vars = ["OPENROUTER_API", "HTTPS_PROXY", "SSL_CERT_FILE", "CODEX_PROXY_CERT"]\n'
    tomllib.loads(result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--existing', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--python', required=True)
    parser.add_argument('--binding', required=True)
    args = parser.parse_args()
    try:
        if args.existing is not None and args.existing.is_symlink():
            raise ValueError('symlink_config_rejected')
        existing = args.existing.read_text() if args.existing else ''
        launcher = str(Path(__file__).resolve().with_name('bound_routing_server.py'))
        candidate = prepare(existing, python=args.python, launcher=launcher, binding=args.binding)
        # Exclusive creation refuses replacement, including symlink destinations.
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            output.write(candidate)
        print('Prepared disabled configuration candidate; no live configuration changed.')
    except Exception:
        print('Configuration preparation rejected; check paths and existing server name.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
