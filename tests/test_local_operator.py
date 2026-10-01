"""Only fake injected credentials, fake HTTP and temporary ledgers."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest

from local_operator import (APPROVAL_ID, LocalOperatorConfig, OperatorConfigError, Prices,
    DirectInjectedOpenRouterTransport, build_operator, read_config, normalize_arguments)
from live_pilot import Approval, PilotLedger
from live_routing import LiveRoutingError, payload_fingerprint
from unified_mcp import CredentialUnavailable, create_server
from test_host_adapters import task, snapshot
from test_unified_mcp import function

FAKE='sk-or-v1-'+'FAKE_OFFLINE_NOT_A_KEY_'*3


def fixture(tmp_path,now=150.):
    from decimal import Decimal
    ledger=PilotLedger(tmp_path/'ledger.sqlite',Approval(APPROVAL_ID,'5',frozenset({'TypeSafe'}),now+50,jev_routing_approved=True))
    ledger.claim('previous','verified-charge',Decimal('.046548454'))
    ledger.finish('previous',{'status':'imported'},billed='.046548454')
    request=normalize_arguments('jev_recommend_host_route',task(authorized_destinations=['openrouter.ai'],budget_usd=.02,
         deadline=now+40,context_references=[]))
    args=normalize_arguments('jev_gate_command',{'command':'curl https://example.com'})
    config={'transport':'direct_env','budget_scope':'canonical_cloud','approval_evidence':'existing-synthetic-approval',
       'ledger_path':str(tmp_path/'ledger.sqlite'),
       'ledger_approval_id':APPROVAL_ID,'ledger_cap_usd':'5','minimum_charged_usd':'.046548454',
       'approval_expires_at':now+50,'workspace_roots':[str(tmp_path)],
       'snapshot':snapshot(evidence_status='runtime_observed',observed_at=now-1,expires_at=now+45),
       'catalog_evidence':'offline-observed-catalog',
       'prices':{'observed_at':now-1,'expires_at':now+45,'evidence':'verified-offline-price',
          'actual_models':['typesafe/jev-1.13'],'context_tokens':32000,'prompt_per_token':'.000000042',
          'completion_per_token':'0','cache_read_per_token':'0','cache_write_per_token':'0',
          'request_fee':'0','other_fees_upper_usd':'0'},
       'tasks':[{'task_sha256':payload_fingerprint(request),'expires_at':now+45,'synthetic_attested':True}],
       'operations':[{'tool':'jev_gate_command','arguments_sha256':payload_fingerprint(args),
          'operation_id':'approved-gate','ceiling_usd':'.01','expires_at':now+45,
          'synthetic_attested':True,'public_metadata_approved':True}]}
    file=tmp_path/'operator.json';file.write_text(json.dumps(config));file.chmod(0o600)
    calls=[]
    def factory(provider,config):
        assert provider()==FAKE
        def send(payload,timeout):
            calls.append(payload)
            answers={}
            for key,q in payload['questions'].items():
                if q['type']=='choice':
                    choice='review' if 'review' in q['criteria'] else next(iter(q['criteria']))
                    answers[key]={'type':'choice','choice':choice,'confidence':1.,'probabilities':{k:float(k==choice) for k in q['criteria']}}
                elif q['type']=='score':answers[key]={'type':'score','score':1.,'confidence':1.}
                else:answers[key]={'type':'noul','noul':.01 if key=='__canary_no' else .99}
            return {'id':'gen-local-operator','provider':'TypeSafe','model':'typesafe/jev-1.13',
                    'usage':{'input_tokens':5,'output_tokens':1,'cost':.001},'answers':answers}
        return send
    return file,config,request,args,calls,factory,ledger


def test_concrete_config_runs_route_gate_without_user_callbacks(tmp_path):
    file,c,request,args,calls,factory,ledger=fixture(tmp_path)
    operator=build_operator(file,credential_provider=lambda:FAKE,transport_factory=factory,clock=lambda:150.)
    server=create_server(operator)
    route=function(server,'jev_recommend_host_route')(request)
    assert route['status']=='recommended'
    assert function(server,'jev_gate_command')(**args)['source']=='model'
    assert len(calls)==2 and ledger.snapshot()['charged_usd']=='0.048548454'
    assert FAKE not in json.dumps(ledger.snapshot())
    assert FAKE not in json.dumps(calls)


def test_unapproved_task_or_tool_never_sends(tmp_path):
    file,c,request,args,calls,factory,ledger=fixture(tmp_path)
    server=create_server(build_operator(file,credential_provider=lambda:FAKE,transport_factory=factory,clock=lambda:150.))
    request['purpose']='Changed unapproved task'
    with pytest.raises(LiveRoutingError):function(server,'jev_recommend_host_route')(request)
    with pytest.raises(LiveRoutingError):function(server,'jev_gate_command')('curl https://other.example')
    assert calls==[]


@pytest.mark.parametrize('change',['stale','synthetic_catalog','missing_fee','negative_price','reset_floor','other_provider','unknown_field'])
def test_config_failures_before_credential_lookup(tmp_path,change):
    file,c,*_=fixture(tmp_path)
    if change=='stale':c['prices']['expires_at']=149.
    if change=='synthetic_catalog':c['snapshot']['evidence_status']='synthetic'
    if change=='missing_fee':del c['prices']['request_fee']
    if change=='negative_price':c['prices']['request_fee']='-1'
    if change=='reset_floor':c['minimum_charged_usd']='0'
    if change=='other_provider':c['prices']['actual_models']=['other/model']
    if change=='unknown_field':c['api_key']='never-allowed'
    file.write_text(json.dumps(c))
    with pytest.raises(OperatorConfigError):build_operator(file,credential_provider=lambda:pytest.fail('credential lookup'),clock=lambda:150.)


@pytest.mark.parametrize('fake',['','op://Vault/Item/credential','issued-proxy-placeholder','sk-or-v1-'+'proxy_placeholder_'*4])
def test_secret_references_and_placeholders_rejected(tmp_path,fake):
    file,*_=fixture(tmp_path)
    with pytest.raises(CredentialUnavailable):build_operator(file,credential_provider=lambda:fake,clock=lambda:150.)


def test_config_refresh_revokes_and_session_change_rejected(tmp_path):
    file,c,request,args,calls,factory,ledger=fixture(tmp_path)
    server=create_server(build_operator(file,credential_provider=lambda:FAKE,transport_factory=factory,clock=lambda:150.))
    c['operations']=[];file.write_text(json.dumps(c))
    with pytest.raises(LiveRoutingError):function(server,'jev_gate_command')(**args)
    c['snapshot']['session_id']='new-session';file.write_text(json.dumps(c))
    with pytest.raises(OperatorConfigError):function(server,'jev_recommend_host_route')(request)
    assert calls==[]


def test_direct_transport_exact_endpoint_no_proxy_or_fallback(monkeypatch):
    captured=[]
    class Reply:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,n):return b'{"ok":true}'
    class Opener:
        def open(self,request,timeout):
            assert request.full_url=='https://openrouter.ai/api/v1/systemone'
            assert request.get_header('Authorization')=='Bearer '+FAKE
            captured.append(json.loads(request.data))
            return Reply()
    import local_operator as local
    handlers=[]
    monkeypatch.setattr(local.urllib.request,'build_opener',lambda *args:handlers.extend(args) or Opener())
    transport=DirectInjectedOpenRouterTransport(credential=lambda:FAKE)
    assert transport({'model':'jev-latest'},1)=={'ok':True}
    assert handlers[0].proxies=={}
    with pytest.raises(LiveRoutingError):transport({'model':'other'},1)
    assert len(captured)==1


def test_full_context_pricing_includes_all_rates_and_fees(tmp_path):
    _,c,*_=fixture(tmp_path)
    p=c['prices'];p.update(prompt_per_token='.001',completion_per_token='.002',cache_read_per_token='.003',
                         cache_write_per_token='.004',request_fee='.5',other_fees_upper_usd='.25',context_tokens=10)
    assert Prices.model_validate(p).quote({},150.,180.).ceiling_usd=='0.850'


def test_cli_check_does_not_touch_credential_or_ledger(tmp_path):
    file,c,*_=fixture(tmp_path,now=time.time())
    Path(c['ledger_path']).unlink()
    root=Path(__file__).resolve().parents[1]
    env={'PATH':'/usr/bin:/bin','JEV_ENV_FILE':'/dev/null','PYTHONDONTWRITEBYTECODE':'1'}
    result=subprocess.run([sys.executable,str(root/'scripts/unified_mcp_server.py'),'--config',str(file),'--check-config'],
                          env=env,capture_output=True,text=True,timeout=5)
    assert result.returncode==0
    assert json.loads(result.stdout)=={'status':'config_valid','credentials_checked':False,'ledger_checked':False}
    assert not Path(c['ledger_path']).exists()


def test_lowered_operation_cap_stops_next_batch(tmp_path):
    file,c,request,args,calls,factory,ledger=fixture(tmp_path)
    root=tmp_path/'public';root.mkdir()
    for n in range(60):(root/f'f{n}.py').write_text(f'def f{n}(): pass')
    c['workspace_roots']=[str(root)]
    args=normalize_arguments('jev_select_context',{'goal':'goal','root':str(root),'globs':['*.py']})
    c['operations']=[{'tool':'jev_select_context','arguments_sha256':payload_fingerprint(args),
        'operation_id':'context','ceiling_usd':'.02','expires_at':195.,
        'synthetic_attested':True,'public_metadata_approved':True}]
    file.write_text(json.dumps(c))
    def transport_factory(provider,config):
        original=factory(provider,config)
        def send(payload,timeout):
            body=original(payload,timeout)
            c['operations'][0]['ceiling_usd']='.001'
            file.write_text(json.dumps(c))
            return body
        return send
    server=create_server(build_operator(file,credential_provider=lambda:FAKE,transport_factory=transport_factory,clock=lambda:150.))
    with pytest.raises(LiveRoutingError):function(server,'jev_select_context')(**args)
    assert len(calls)==1
    assert ledger.snapshot()['charged_usd']=='0.047548454'
    assert ledger.snapshot()['halted']


def test_real_stdio_initialize_and_list_with_fake_injection(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    file,c,*_=fixture(tmp_path,now=time.time())
    root=Path(__file__).resolve().parents[1]
    # No tool call, no model I/O. The standalone process consumes a FAKE token.
    params=StdioServerParameters(command=sys.executable,
        args=[str(root/'scripts/unified_mcp_server.py'),'--config',str(file)],
        env={'JEV_OPENROUTER_TOKEN':FAKE,'JEV_ENV_FILE':'/dev/null','PYTHONDONTWRITEBYTECODE':'1'})
    async def probe():
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                listed=await session.list_tools()
                assert {t.name for t in listed.tools}=={'jev_recommend_host_route','jev_gate_command','jev_select_context'}
    asyncio.run(asyncio.wait_for(probe(),timeout=10))


def test_separate_smoke_ledger_needs_explicit_authority_and_never_overwrites(tmp_path):
    from local_operator import initialize_smoke_ledger
    file,c,*_=fixture(tmp_path)
    original=Path(c['ledger_path']).read_bytes()
    with pytest.raises(OperatorConfigError):initialize_smoke_ledger(file,clock=lambda:150.)
    c.update(budget_scope='local_synthetic_smoke',ledger_approval_id='mac-separate-test-approval',
             ledger_cap_usd='.10',minimum_charged_usd='0',ledger_path=str(tmp_path/'smoke.sqlite'))
    file.write_text(json.dumps(c))
    with pytest.raises(OperatorConfigError):initialize_smoke_ledger(file,clock=lambda:150.)
    assert not Path(c['ledger_path']).exists()
    c['ledger_creation_authorized']=True;file.write_text(json.dumps(c))
    result=initialize_smoke_ledger(file,clock=lambda:150.)
    assert result['cap_usd']=='.10' and result['model_calls']==0
    assert Path(c['ledger_path']).stat().st_mode & 0o777==0o600
    with pytest.raises(FileExistsError):initialize_smoke_ledger(file,clock=lambda:150.)
    assert (tmp_path/'ledger.sqlite').read_bytes()==original
    c['ledger_cap_usd']='.11';file.write_text(json.dumps(c))
    with pytest.raises(OperatorConfigError):read_config(file,clock=lambda:150.)


def test_concrete_context_adapter(tmp_path):
    file,c,request,args,calls,factory,ledger=fixture(tmp_path)
    root=tmp_path/'public';root.mkdir();(root/'one.py').write_text('def public_name(): return "LOCAL_SOURCE_ONLY"')
    c['workspace_roots']=[str(root)]
    args=normalize_arguments('jev_select_context',{'goal':'Find public function','root':str(root),'globs':['*.py']})
    c['operations']=[{'tool':'jev_select_context','arguments_sha256':payload_fingerprint(args),
       'operation_id':'context','ceiling_usd':'.01','expires_at':195.,
       'synthetic_attested':True,'public_metadata_approved':True}]
    file.write_text(json.dumps(c))
    server=create_server(build_operator(file,credential_provider=lambda:FAKE,transport_factory=factory,clock=lambda:150.))
    assert function(server,'jev_select_context')(**args)['include']==['one.py']
    assert len(calls)==1 and 'LOCAL_SOURCE_ONLY' not in json.dumps(calls)
    assert ledger.snapshot()['charged_usd']=='0.047548454'


def test_shipped_example_cannot_activate():
    from pydantic import ValidationError
    root=Path(__file__).resolve().parents[1]
    with pytest.raises(ValidationError):LocalOperatorConfig.model_validate(json.loads((root/'config/local-operator.example.json').read_text()))


def test_direct_mode_only_reads_agreed_injected_variable(tmp_path,monkeypatch):
    import local_operator
    file,*_=fixture(tmp_path)
    class HostEnvironment:
        def get(self,name):
            assert name=='JEV_OPENROUTER_TOKEN'
            return None
    real_os=local_operator.os
    class HostOS:
        environ=HostEnvironment()
        def __getattr__(self,name):return getattr(real_os,name)
    monkeypatch.setattr(local_operator,'os',HostOS())
    with pytest.raises(CredentialUnavailable):build_operator(file,clock=lambda:150.)
