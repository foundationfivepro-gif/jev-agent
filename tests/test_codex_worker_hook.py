import copy
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from codex_operator import SYNTHETIC_MESSAGE, ACCEPTANCE, ROOT, CodexModelDiscovery, authorize_synthetic
from codex_worker_hook import handle
from host_contracts import TaskEnvelope


def event():
    return {'hook_event_name':'PreToolUse','tool_name':'spawn_agent','cwd':str(ROOT),
        'session_id':'session-1','turn_id':'turn-1','tool_use_id':'call-1',
        'tool_input':{'message':SYNTHETIC_MESSAGE,'fork_context':False}}


def factory(calls, **changes):
    class Host:
        def recommend(self, request):
            calls.append(request)
            return SimpleNamespace(status='recommended',routing_evidence_status='jev_observed',
                routing_cost_kind='billed',execution_mode='delegate',selected_model_namespace='openai_native',
                selected_model_id=changes.get('model','gpt-6-luna'),selected_effort=changes.get('effort','low'),
                routing_decision_id='gen-live-fixture')
    return lambda **kwargs:Host()


def test_handler_real_codex_v1_schema_recommends_but_does_not_authorize_execution():
    calls=[]
    result=handle(event(),host_factory=factory(calls),clock=lambda:150.)['hookSpecificOutput']
    assert result['permissionDecision']=='deny'
    assert 'updatedInput' not in result
    assert json.loads(result['additionalContext'])['model']=='gpt-6-luna'
    assert json.loads(result['additionalContext'])['execution_authorized'] is False
    assert calls[0]['purpose']==SYNTHETIC_MESSAGE
    assert authorize_synthetic(TaskEnvelope.model_validate(calls[0]))


def test_handler_preserves_explicit_choice_as_routing_constraints():
    e=event();e['tool_input'].update(model='gpt-6.1-sol',reasoning_effort='high')
    calls=[]
    result=handle(e,host_factory=factory(calls,model='gpt-6.1-sol',effort='high'),clock=lambda:150.)
    assert calls[0]['explicit_model_id']=='gpt-6.1-sol'
    assert calls[0]['explicit_effort']=='high'
    assert result['hookSpecificOutput']['permissionDecision']=='deny'


@pytest.mark.parametrize('mutation',[
    lambda e:e.update(cwd='/tmp'), lambda e:e.update(tool_name='Bash'),
    lambda e:e.update(hook_event_name='SubagentStart'), lambda e:e.update(turn_id=None),
    lambda e:e['tool_input'].update(message='Private task content'),
    lambda e:e['tool_input'].update(fork_context=True),
    lambda e:e['tool_input'].update(fork_context=0),
    lambda e:e['tool_input'].update(agent_type='privileged-role'),
    lambda e:e['tool_input'].update(fork_turns='all'),
    lambda e:e['tool_input'].update(items=[{'type':'text','text':'Private'}]),
])
def test_outside_approved_scope_explicitly_denies_before_binding(mutation):
    e=event();mutation(e)
    result=handle(e,host_factory=lambda **kwargs:pytest.fail('unauthorized binding'))
    assert result['hookSpecificOutput']['permissionDecision']=='deny'
    assert 'Private' not in json.dumps(result)


def test_binding_failure_is_explicit_deny_not_hook_error():
    def fail(**kwargs):raise RuntimeError('secret-shaped upstream exception')
    result=handle(event(),host_factory=fail)
    assert result['hookSpecificOutput']['permissionDecision']=='deny'
    assert 'secret-shaped' not in json.dumps(result)


def test_executable_handler_malformed_stdin_returns_valid_deny():
    p=subprocess.run([sys.executable,str(ROOT/'codex_worker_hook.py')],input='not json',
                     text=True,capture_output=True,timeout=5)
    assert p.returncode==0
    assert json.loads(p.stdout)['hookSpecificOutput']['permissionDecision']=='deny'
    assert not p.stderr


def test_readonly_model_discovery_fresh_process_and_no_model_turn(tmp_path):
    script=tmp_path/'fake_app_server.py'
    script.write_text('''import sys,json
for line in sys.stdin:
    request=json.loads(line)
    method=request['method']
    assert method in ['initialize','initialized','model/list']
    if method=='initialized':continue
    if method=='initialize':
        assert request['params']['capabilities']['explicitGatewayOauth'] is True
        result={}
    else:
        result={'data':[{'model':'gpt-6-luna','supportedReasoningEfforts':[{'reasoningEffort':'low'}]}],'nextCursor':None}
    print(json.dumps({'id':request['id'],'result':result}),flush=True)
''')
    models=CodexModelDiscovery(command=(sys.executable,str(script)),cwd=tmp_path).read()
    assert models[0]['model_id']=='gpt-6-luna' and models[0]['efforts']==['low']


def test_discovery_startup_failure_has_no_raw_diagnostics(tmp_path):
    from live_routing import LiveRoutingError
    with pytest.raises(LiveRoutingError,match='live_routing_unavailable'):
        CodexModelDiscovery(command=(sys.executable,'-c','raise Exception("private-data")'),cwd=tmp_path).read()


def test_activation_is_proposal_not_live_registration():
    import tomllib
    activation=tomllib.loads((ROOT/'config/codex-local-activation.toml').read_text())
    assert activation['hooks']['PreToolUse'][0]['matcher']=='^(Agent|spawn_agent)$'
    assert activation['mcp_servers']['jev_bound']['required'] is True
    assert not (ROOT/'.codex/config.toml').exists()


@pytest.mark.parametrize('change', ['valid','request_fee','missing_fee','wrong_identity','extra_fee'])
def test_routing_quote_requires_complete_zero_auxiliary_fees(monkeypatch, change):
    import codex_operator as op
    from live_routing import LiveRoutingError
    pricing={'prompt':'0.000000042','completion':'0','request':'0',
             'input_cache_read':'0','input_cache_write':'0'}
    data={'id':'typesafe/jev-1.13-20260917','endpoints':[{'provider_name':'TypeSafe',
          'status':0,'model_id':'typesafe/jev-1.13','context_length':32000,'pricing':pricing}]}
    if change=='request_fee': pricing['request']='1'
    if change=='missing_fee': del pricing['request']
    if change=='wrong_identity': data['id']='untrusted/model'
    if change=='extra_fee': pricing['image']='.1'
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return json.dumps({'data':data}).encode()
    monkeypatch.setenv('HTTPS_PROXY','http://localhost:9999')
    monkeypatch.setattr(op.urllib.request,'build_opener',lambda *args:SimpleNamespace(open=lambda *a,**kw:Response()))
    calls=[]
    handle(event(),host_factory=factory(calls),clock=lambda:150.)
    task=TaskEnvelope.model_validate(calls[0])
    if change=='valid':
        assert op.quote_routing(task,{}).ceiling_usd=='0.001344000'
    else:
        with pytest.raises(LiveRoutingError):op.quote_routing(task,{})
