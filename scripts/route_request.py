#!/usr/bin/env python3
"""Operator-only JSON request -> validated JEV receipt; never dispatches."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel, ConfigDict, Field
from host_contracts import CapabilitySnapshot, TaskEnvelope
from live_routing import (BoundHostRouting, OpenRouterSelector, ProtectedOpenRouterTransport,
                          RoutingAuthorization, LiveRoutingError, payload_fingerprint)
from live_pilot import Approval, PilotLedger
from codex_operator import LEDGER, APPROVAL_ID


class OperatorAuthorization(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    approved_task_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    snapshot: CapabilitySnapshot
    expires_at: float
    routing_ceiling_usd: str
    actual_router_models: list[str]
    pricing_evidence: str


def read_json(stream):
    raw = stream.read(65537)
    if len(raw) > 65536:
        raise LiveRoutingError()
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise LiveRoutingError()
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(LiveRoutingError()))


def read_authorization(path):
    # This file is operator authority, separate from untrusted task JSON. Never
    # expose its path as an MCP argument or let a task author write the file.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise LiveRoutingError()
        return OperatorAuthorization.model_validate(read_json(stream))


def recommend(task, authority, *, transport=None, ledger_path=LEDGER, clock=time.time):
    from decimal import Decimal
    now = clock()
    task = TaskEnvelope.model_validate(task)
    authority = OperatorAuthorization.model_validate(authority)
    snapshot = authority.snapshot
    digest = payload_fingerprint(task.model_dump(mode='json'))
    if (digest != authority.approved_task_sha256 or task.data_class != 'public'
            or task.authorized_destinations != ['openrouter.ai'] or task.context_references
            or not now < authority.expires_at <= now + 300
            or not snapshot.observed_at <= now < snapshot.expires_at <= authority.expires_at
            or snapshot.evidence_status != 'runtime_observed'
            or not now < task.deadline <= authority.expires_at):
        raise LiveRoutingError()
    path = Path(ledger_path)
    if not path.is_file() or path.is_symlink():
        raise LiveRoutingError()
    ledger = PilotLedger(path, Approval(APPROVAL_ID, '5', frozenset({'TypeSafe'}),
                                      authority.expires_at, jev_routing_approved=True))
    if Decimal(ledger.snapshot()['charged_usd']) < Decimal('0.046548454'):
        raise LiveRoutingError()
    if transport is None:
        transport = ProtectedOpenRouterTransport(
            issued_placeholder=lambda: os.environ['OPENROUTER_API'],
            https_proxy=os.environ['HTTPS_PROXY'])
    def authorize_quote(bound_task, payload):
        if clock() >= authority.expires_at or payload_fingerprint(bound_task.model_dump(mode='json')) != digest:
            raise LiveRoutingError()
        return RoutingAuthorization(payload_fingerprint(payload), authority.routing_ceiling_usd,
            authority.expires_at, frozenset(authority.actual_router_models), authority.pricing_evidence)
    selector = OpenRouterSelector(transport=transport, ledger=ledger,
                                  authorize=authorize_quote, clock=clock)
    host = BoundHostRouting(runtime_id=snapshot.runtime_id, session_id=snapshot.session_id,
        discover=lambda: snapshot, selector=selector.binding(), clock=clock,
        authorize=lambda t: payload_fingerprint(t.model_dump(mode='json')) == digest)
    return host.recommend(task).model_dump(mode='json')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--authorization', help='Private operator-owned authorization JSON path')
    group.add_argument('--fingerprint', action='store_true', help='Validate request and print canonical task hash; no network')
    args = parser.parse_args(argv)
    try:
        task = TaskEnvelope.model_validate(read_json(sys.stdin))
        if args.fingerprint:
            output = {'task_sha256': payload_fingerprint(task.model_dump(mode='json'))}
        else:
            output = recommend(task, read_authorization(args.authorization))
        print(json.dumps(output, allow_nan=False, separators=(',', ':')))
        return 0
    except Exception:
        print('{"error":"live_routing_unavailable"}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
