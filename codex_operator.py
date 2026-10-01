"""Concrete local Codex synthetic-pilot binding; no activation on import.

Only this checked-in synthetic task may leave the process under the existing
approval. Live host models are discovered by read-only app-server model/list.
A missing ledger, credentials, catalog, scope or capability fails closed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import time
import urllib.request
import uuid

from host_adapters import NATIVE_FAMILIES
from live_pilot import Approval, PilotLedger
from live_routing import (BoundHostRouting, LiveRoutingError, OpenRouterSelector,
                         ProtectedOpenRouterTransport, RoutingAuthorization,
                         _ForcedProxy, _NoRedirects, payload_fingerprint)

ROOT = Path(__file__).resolve().parent
LEDGER = Path('/workspace/scratch/jev-canonical-budget.sqlite')
APPROVAL_ID = 'approved-synthetic-cap-2026-10-01'
SYNTHETIC_MESSAGE = ('Synthetic native worker verification. Sort the integers [7, 2, 9, 2] ascending and compute their sum. '
                     'Return only JSON with keys sorted and sum. Do not use tools or make external changes.')
ACCEPTANCE = ['Return exactly {"sorted":[2,2,7,9],"sum":20}.',
              'No tools, files, network requests, or external side effects.']
JEV_CATALOG_URL = 'https://openrouter.ai/api/v1/models/typesafe/jev-1.13-20260917/endpoints'


class CodexModelDiscovery:
    """Fresh app-server connection; never starts a thread/turn or opens OAuth."""
    def __init__(self, *, command=('codex','app-server','--stdio'), cwd=ROOT, timeout=10):
        self.command, self.cwd, self.timeout = tuple(command), Path(cwd), timeout

    def read(self):
        process = None
        try:
            process = subprocess.Popen(self.command, cwd=self.cwd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=False)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            pending = b''
            expires = time.monotonic() + self.timeout
            def send(message):
                process.stdin.write(json.dumps(message).encode() + b'\n')
                process.stdin.flush()
            def receive(identifier):
                nonlocal pending
                while time.monotonic() < expires:
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        reply = json.loads(line)
                        if reply.get('id') == identifier:
                            if 'error' in reply:
                                raise LiveRoutingError()
                            return reply['result']
                    if len(pending) > 1_000_000:
                        raise LiveRoutingError()
                    if selector.select(min(0.2, max(0, expires-time.monotonic()))):
                        block = os.read(process.stdout.fileno(), 65536)
                        if not block:
                            raise LiveRoutingError()
                        pending += block
                raise LiveRoutingError()
            send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'jev-local-discovery','version':'1'},
                'capabilities':{'experimentalApi':True,'explicitGatewayOauth':True}}})
            receive(1)
            send({'method':'initialized'})
            send({'id':2,'method':'model/list','params':{'limit':100,'includeHidden':False}})
            reply = receive(2)
            if reply.get('nextCursor'):
                raise LiveRoutingError()
            models = []
            for row in reply['data']:
                model_id = row['model']
                if model_id not in NATIVE_FAMILIES or row.get('hidden', False):
                    continue
                efforts = [item['reasoningEffort'] for item in row['supportedReasoningEfforts']]
                models.append({'model_id':model_id,'namespace':'openai_native','family':NATIVE_FAMILIES[model_id],
                    'efforts':efforts,'supported_tools':[],'available':True})
            if not models:
                raise LiveRoutingError()
            return models
        except Exception:
            raise LiveRoutingError() from None
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
                if 'selector' in locals():
                    selector.close()


def authorize_synthetic(task):
    return (task.purpose == SYNTHETIC_MESSAGE and task.acceptance_criteria == ACCEPTANCE
        and task.data_class == 'public' and task.authorized_destinations == ['openrouter.ai']
        and not task.context_references and not task.required_tools
        and not task.failed_acceptance_checks and task.budget_usd is not None
        and 0 < task.budget_usd <= .002)


def quote_routing(task, payload):
    """Public metadata GET, full-context ceiling, exact resolved identities."""
    from decimal import Decimal
    if not authorize_synthetic(task):
        raise LiveRoutingError()
    try:
        opener = urllib.request.build_opener(
            _ForcedProxy({'https':os.environ['HTTPS_PROXY']}, allowed_urls={JEV_CATALOG_URL}), _NoRedirects())
        with opener.open(JEV_CATALOG_URL, timeout=5) as response:
            raw=response.read(262145)
            if len(raw)>262144:
                raise LiveRoutingError()
            data=json.loads(raw)['data']
        if data['id'] != 'typesafe/jev-1.13-20260917':
            raise LiveRoutingError()
        endpoint=next(e for e in data['endpoints'] if e['provider_name']=='TypeSafe' and e['status']==0)
        if endpoint['model_id'] != 'typesafe/jev-1.13':
            raise LiveRoutingError()
        pricing=endpoint['pricing']
        # Missing fee metadata is not evidence of zero. Refuse a partial quote.
        for key in ('request','input_cache_read','input_cache_write'):
            if key not in pricing or Decimal(pricing[key]) != 0:
                raise LiveRoutingError()
        for key,value in pricing.items():
            if key not in {'prompt','completion'} and Decimal(value) != 0:
                raise LiveRoutingError()
        if (type(endpoint['context_length']) is not int or not 0 < endpoint['context_length'] <= 32000
                or Decimal(endpoint['pricing']['completion']) != 0
                or Decimal(endpoint['pricing']['prompt']) != Decimal('0.000000042')):
            raise LiveRoutingError()
        ceiling=Decimal(endpoint['context_length'])*Decimal(endpoint['pricing']['prompt'])
        return RoutingAuthorization(payload_fingerprint(payload),str(ceiling),time.time()+60,
            frozenset({'typesafe/jev-1.13','typesafe/jev-1.13-20260917'}),'openrouter-endpoints-local-pilot')
    except Exception:
        raise LiveRoutingError() from None


def build_host(*, session_id=None, discovery=None):
    """Bind only this local process/repository; callers cannot install dependencies."""
    from decimal import Decimal
    if not LEDGER.is_file() or LEDGER.is_symlink():
        raise LiveRoutingError()
    ledger=PilotLedger(LEDGER, Approval(APPROVAL_ID,'5',frozenset({'TypeSafe'}),time.time()+60,
                                      jev_routing_approved=True))
    if Decimal(ledger.snapshot()['charged_usd']) < Decimal('0.046548454'):
        raise LiveRoutingError()
    runtime_id='local-codex-' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    session_id=session_id or 'stdio-' + uuid.uuid4().hex
    discovery=discovery or CodexModelDiscovery()
    snapshot = None
    def discover():
        nonlocal snapshot
        if snapshot is None or time.time() >= snapshot['expires_at']:
            observed=time.time()
            models=discovery.read()
            snapshot={'surface':'codex_local','runtime_id':runtime_id,'session_id':session_id,
                'observed_at':observed,'expires_at':observed+60,'available':True,'observed_tools':[],
                'dispatch_controls':['delegate','model_override','effort_override'],'models':models,
                'evidence_status':'runtime_observed'}
        return snapshot
    discover()
    transport=ProtectedOpenRouterTransport(issued_placeholder=lambda:os.environ['OPENROUTER_API'],
                                          https_proxy=os.environ['HTTPS_PROXY'])
    binding=OpenRouterSelector(transport=transport,ledger=ledger,authorize=quote_routing).binding()
    return BoundHostRouting(runtime_id=runtime_id,session_id=session_id,discover=discover,
                            selector=binding,authorize=authorize_synthetic)
