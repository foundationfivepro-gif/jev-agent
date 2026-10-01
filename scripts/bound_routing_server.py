#!/usr/bin/env python3
"""Launch a separately reviewed host binding. No raw-key or .env loading."""
import argparse
import importlib.util
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_host(path):
    from live_routing import BoundHostRouting
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('trusted_binding_required')
    spec = importlib.util.spec_from_file_location('jev_operator_binding', path)
    if spec is None or spec.loader is None:
        raise ValueError('trusted_binding_required')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    host = module.build_host()
    if type(host) is not BoundHostRouting:
        raise ValueError('trusted_binding_required')
    return host


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binding', required=True)
    args = parser.parse_args()
    try:
        from live_routing import create_routing_server
        create_routing_server(load_host(args.binding)).run()
    except Exception:
        print('JEV host binding unavailable; no fallback activated.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
