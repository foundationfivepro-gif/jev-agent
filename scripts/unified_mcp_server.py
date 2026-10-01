#!/usr/bin/env python3
"""Launch a reviewed local binding; never discover credentials or create a ledger."""
import argparse
import importlib.util
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unified_mcp import create_server, UnifiedOperator, CredentialUnavailable


def load_operator(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError()
    spec = importlib.util.spec_from_file_location('jev_unified_operator', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    operator = module.build_operator()
    if type(operator) is not UnifiedOperator:
        raise ValueError()
    return operator


def main():
    parser = argparse.ArgumentParser()
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--binding',help='Advanced reviewed Python binding')
    group.add_argument('--config',help='Private JSON operator config; no custom callbacks required')
    group.add_argument('--schema',action='store_true',help='Print config JSON schema without credential or ledger access')
    group.add_argument('--fingerprint',choices=['jev_recommend_host_route','jev_gate_command','jev_select_context'])
    parser.add_argument('--check-config',action='store_true',help='Validate JSON freshness only; no credential/ledger access')
    parser.add_argument('--initialize-ledger',action='store_true',help='Explicit separately approved local smoke setup; refuses existing files and canonical cloud budget')
    args = parser.parse_args()
    try:
        from local_operator import LocalOperatorConfig, read_config, build_operator, normalize_arguments
        from live_routing import payload_fingerprint
        import json
        if args.schema:
            print(json.dumps(LocalOperatorConfig.model_json_schema()))
        elif args.fingerprint:
            from scripts.route_request import read_json
            print(json.dumps({'sha256':payload_fingerprint(normalize_arguments(args.fingerprint,read_json(sys.stdin)))}))
        elif args.initialize_ledger:
            if not args.config or args.check_config:raise ValueError()
            from local_operator import initialize_smoke_ledger
            print(json.dumps(initialize_smoke_ledger(args.config)))
        elif args.check_config:
            if not args.config:raise ValueError()
            read_config(args.config)
            print('{"status":"config_valid","credentials_checked":false,"ledger_checked":false}')
        else:
            create_server(build_operator(args.config) if args.config else load_operator(args.binding)).run()
    except CredentialUnavailable as exc:
        print(str(exc),file=sys.stderr)
        return 1
    except Exception:
        print('unified_binding_unavailable: verify reviewed operator paths, existing ledger, authorization and host discovery; no fallback activated',file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
