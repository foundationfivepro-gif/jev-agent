import asyncio
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import pytest

from core import Noul
from live_pilot import Approval, PilotLedger
from live_routing import LiveRoutingError, RoutingAuthorization, payload_fingerprint
from unified_mcp import (AccountedDecisions, OperationGrant, UnifiedOperator, create_server,
                         protected_transport, CredentialUnavailable, open_existing_ledger)
from test_host_adapters import task, snapshot


def setup(tmp_path):
    approval=Approval('unified-test','5',frozenset({'TypeSafe'}),200.,jev_routing_approved=True)
    ledger=PilotLedger(tmp_path/'ledger.sqlite',approval)
    calls=[]
    def send(payload,timeout):
        calls.append(payload)
        answers={}
        for key,q in payload['questions'].items():
            if q['type']=='noul':answers[key]={'type':'noul','noul':.01 if key=='__canary_no' else .99}
            elif q['type']=='score':answers[key]={'type':'score','score':1.,'confidence':.99}
            else:
                choices=q.get('criteria',{})
                selected='review' if 'review' in choices else next(iter(choices))
                answers[key]={'type':'choice','choice':selected,'confidence':.99,
                              'probabilities':{c:1. if c==selected else 0. for c in choices}}
        return {'id':'gen-test-'+str(len(calls)),'provider':'TypeSafe','model':'typesafe/jev-1.13',
                'answers':answers,'usage':{'input_tokens':10,'output_tokens':2,'cost':.001}}
    def quote(tool,args,payload):
        return RoutingAuthorization(payload_fingerprint(payload),'.002',200.,
                                    frozenset({'typesafe/jev-1.13'}),'offline-full-price-fixture')
    def authorize(tool,args):
        return OperationGrant(tool+'-'+payload_fingerprint(args),payload_fingerprint(args),'.02',200.)
    op=UnifiedOperator(ledger,send,'runtime-1','session-1',
        lambda:snapshot(evidence_status='runtime_observed'),lambda task:True,
        lambda task,payload:quote('route',{},payload),authorize,quote,(tmp_path,),lambda:150.)
    return op,calls,approval


def function(server,name):
    return server._tool_manager.get_tool(name).fn


def test_exact_three_tools_and_real_bound_route(tmp_path):
    op,calls,_=setup(tmp_path);server=create_server(op)
    names={t.name for t in asyncio.run(server.list_tools())}
    assert names=={'jev_recommend_host_route','jev_gate_command','jev_select_context'}
    result=function(server,'jev_recommend_host_route')(task(authorized_destinations=['openrouter.ai'],budget_usd=.02))
    assert result['status']=='recommended' and result['routing_evidence_status']=='jev_observed'
    assert len(calls)==1


def test_gate_reuses_policy_and_accounts_model_request(tmp_path):
    op,calls,_=setup(tmp_path);fn=function(create_server(op),'jev_gate_command')
    assert fn('rm -rf /')['decision']=='block'
    assert fn('git status')['source']=='policy'
    assert calls==[]
    result=fn('curl https://example.com',str(tmp_path))
    assert result['source']=='model'
    assert fn('curl https://example.com',str(tmp_path))==result
    assert len(calls)==1 and op.ledger.snapshot()['charged_usd']=='0.001'


def test_context_batches_and_refinement_share_reservation_no_source_leaves(tmp_path):
    op,calls,_=setup(tmp_path)
    for n in range(60):(tmp_path/f'file{n}.py').write_text(f'def name_{n}():\n    return "SOURCE_BODY_MARKER"\n')
    fn=function(create_server(op),'jev_select_context')
    result=fn('Find relevant public functions',str(tmp_path),['*.py'])
    assert result['include'] and len(calls)>=3
    assert 'SOURCE_BODY_MARKER' not in json.dumps(calls)
    assert op.ledger.snapshot()['charged_usd']==str(Decimal('.001')*len(calls))
    assert op.ledger.snapshot()['reserved_usd']=='0'
    count=len(calls)
    assert fn('Find relevant public functions',str(tmp_path),['*.py'])==result
    assert len(calls)==count
    (tmp_path/'file0.py').write_text('def changed(): pass')
    with pytest.raises(LiveRoutingError):fn('Find relevant public functions',str(tmp_path),['*.py'])
    assert len(calls)==count


def test_context_wrong_root_denied_before_read_or_send(tmp_path):
    op,calls,_=setup(tmp_path)
    with pytest.raises(ValueError):function(create_server(op),'jev_select_context')('goal','/etc')
    assert calls==[]


@pytest.mark.parametrize('failure',['timeout','bad_identity','overquote','expired','unknown_cost'])
def test_failures_halt_without_retry(tmp_path,failure):
    op,calls,_=setup(tmp_path);original=op.transport
    def send(payload,timeout):
        body=original(payload,timeout)
        if failure=='timeout':raise OSError('secret-shaped error')
        if failure=='bad_identity':body['model']='other/model'
        if failure=='unknown_cost':del body['usage']['cost']
        return body
    op.transport=send
    if failure=='overquote':
        op.quote_decision=lambda t,a,p:RoutingAuthorization(payload_fingerprint(p),'1',200.,frozenset({'typesafe/jev-1.13'}),'fixture')
    if failure=='expired':op.authorize_operation=lambda t,a:OperationGrant('expired',payload_fingerprint(a),'.02',149.)
    fn=function(create_server(op),'jev_gate_command')
    with pytest.raises(LiveRoutingError):fn('curl https://example.com')
    with pytest.raises(LiveRoutingError):fn('curl https://example.com')
    assert len(calls)==(0 if failure in {'overquote','expired'} else 1)
    if failure in {'timeout','unknown_cost'}:
        assert op.ledger.snapshot()['reserved_usd']=='0.02'
    if failure!='expired':assert op.ledger.snapshot()['halted']


def test_refinement_failure_is_not_swallowed(tmp_path):
    op,calls,_=setup(tmp_path)
    for n in range(26):(tmp_path/f'f{n}.py').write_text(f'def f{n}(): pass')
    original=op.transport
    def send(payload,timeout):
        if calls:raise OSError('refinement timeout')
        return original(payload,timeout)
    op.transport=send
    with pytest.raises(LiveRoutingError):function(create_server(op),'jev_select_context')('goal',str(tmp_path),['*.py'])
    assert op.ledger.snapshot()['reserved_usd']=='0.02'
    assert op.ledger.snapshot()['halted']


def test_no_credential_fallback_and_no_new_ledger(tmp_path):
    def unavailable():raise RuntimeError('private provider detail')
    with pytest.raises(CredentialUnavailable,match='credential_unavailable') as exc:protected_transport(unavailable)
    assert 'private provider detail' not in str(exc.value)
    op,_,approval=setup(tmp_path)
    with pytest.raises(LiveRoutingError):open_existing_ledger(tmp_path/'absent.sqlite',approval,minimum_charged_usd='0')
    assert not (tmp_path/'absent.sqlite').exists()
    assert open_existing_ledger(tmp_path/'ledger.sqlite',approval,minimum_charged_usd='0').snapshot()['cap_usd']=='5'


def test_import_and_discovery_never_load_legacy_server_or_env(tmp_path):
    op,_,_=setup(tmp_path)
    import core
    original=core.load_env
    core.load_env=lambda *a,**k:pytest.fail('legacy loader')
    try:assert len(asyncio.run(create_server(op).list_tools()))==3
    finally:core.load_env=original


def test_aggregate_context_limit_before_paid_call(tmp_path,monkeypatch):
    import unified_mcp
    op,calls,_=setup(tmp_path)
    monkeypatch.setattr(unified_mcp,'MAX_CONTEXT_BYTES',20)
    (tmp_path/'a.py').write_text('def a(): return 1234567890')
    with pytest.raises(ValueError,match='scan limit exceeded'):
        function(create_server(op),'jev_select_context')('goal',str(tmp_path),['*.py'])
    assert calls==[] and op.ledger.snapshot()['charged_usd']=='0'


def test_shared_ledger_blocks_route_while_context_is_in_flight(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    op,calls,_=setup(tmp_path)
    (tmp_path/'a.py').write_text('def a(): return 1')
    entered=threading.Event();release=threading.Event();original=op.transport
    def send(payload,timeout):
        entered.set()
        assert release.wait(3)
        return original(payload,timeout)
    op.transport=send
    server=create_server(op)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(function(server,'jev_select_context'),'goal',str(tmp_path),['*.py'])
        try:
            assert entered.wait(3)
            result=function(server,'jev_recommend_host_route')(task(authorized_destinations=['openrouter.ai'],budget_usd=.02))
            assert result['status']!='recommended'
            assert calls==[]
        finally:release.set()
        assert future.result()['include']==['a.py']
    assert len(calls)==1


def test_launcher_reports_missing_provider_without_private_details(tmp_path):
    binding=tmp_path/'binding.py'
    binding.write_text('from unified_mcp import protected_transport\ndef build_operator():\n    def unavailable():\n        raise RuntimeError("private provider detail")\n    return protected_transport(unavailable)\n')
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,str(root/'scripts/unified_mcp_server.py'),'--binding',str(binding)],
                          capture_output=True,text=True,timeout=5)
    assert result.returncode==1 and not result.stdout
    assert 'credential_unavailable' in result.stderr
    assert 'private provider detail' not in result.stderr
    assert 'Traceback' not in result.stderr
