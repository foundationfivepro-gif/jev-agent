import copy
import io
import json
from pathlib import Path
import tomllib

import pytest

from scripts.prepare_codex_install import prepare
from scripts.bound_routing_server import load_host
from live_pilot import ENDPOINT, PilotError, ALIASES
from live_writing import OpenRouterWritingTransport, OpenRouterWritingCatalog


def test_candidate_preserves_existing_config_and_stays_disabled():
    original = '# existing\nmodel = "unchanged"\n[mcp_servers.existing]\ncommand = "keep"\n'
    output = prepare(original, python='/workspace/python', launcher='/workspace/launcher.py', binding='/workspace/binding.py')
    assert output.startswith(original)
    config = tomllib.loads(output)
    assert config['mcp_servers']['existing']['command'] == 'keep'
    bound = config['mcp_servers']['jev_bound']
    assert bound['enabled'] is False
    assert bound['enabled_tools'] == ['jev_recommend_host_route']
    assert 'OPENROUTER_API_KEY' not in output
    assert 'OPENROUTER_API' in bound['env_vars']


def test_existing_binding_is_not_overwritten():
    with pytest.raises(ValueError):
        prepare('[mcp_servers.jev_bound]\ncommand="keep"', python='/p',launcher='/l',binding='/b')


def test_untrusted_missing_or_symlink_binding_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_host(tmp_path / 'absent.py')
    target = tmp_path / 'binding.py'
    target.write_text('raise AssertionError("must not load symlink")')
    link = tmp_path / 'link.py'
    link.symlink_to(target)
    with pytest.raises(ValueError):
        load_host(link)


def test_config_artifacts_are_valid_and_disabled():
    root = Path(__file__).resolve().parents[1]
    for file in (root / 'config').glob('*candidate.toml'):
        config = tomllib.loads(file.read_text())
        assert all(server['enabled'] is False for server in config['mcp_servers'].values())
    design = json.loads((root / 'config/remote-oauth-design.json').read_text())
    assert design['issuer'] is None and design['client_id'] is None
    assert design['initial_requested_scopes'] == ['routing:recommend']


def writing_payload():
    return {'model':'anthropic/claude-sonnet-test','messages':[{'role':'user','content':'Synthetic announcement.'}],
        'max_tokens':20,'stream':False,'provider':{'only':['Anthropic'],'order':['Anthropic'],
            'allow_fallbacks':False,'require_parameters':True,'data_collection':'deny','zdr':True,
            'max_price':{'prompt':'3','completion':'15','request':'0'}}}


def writing_reply():
    return {'id':'gen-fixture','model':'anthropic/claude-sonnet-test','provider':'Anthropic',
        'choices':[{'finish_reason':'stop','message':{'content':'  Exact draft.\n'}}],
        'usage':{'prompt_tokens':10,'completion_tokens':4,'cost':.00009}}


def writer(body=None):
    calls=[]
    transport=OpenRouterWritingTransport(issued_placeholder=lambda:'issued-placeholder',
        https_proxy='http://127.0.0.1:8080',allowed_models={'anthropic/claude-sonnet-test'},allowed_providers={'Anthropic'})
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return io.BytesIO(json.dumps(body if body is not None else writing_reply()).encode())
    transport._opener=Opener()
    return transport,calls


def test_real_writing_adapter_preserves_exact_output_and_usage():
    transport,calls=writer()
    reply=transport.send(endpoint=ENDPOINT,payload=writing_payload(),timeout_seconds=2,operation_id='op')
    assert reply.text == '  Exact draft.\n'
    assert reply.actual_provider == 'Anthropic'
    assert reply.billed_usd == '0.00009'
    assert reply.usage.input_tokens == 10
    assert len(calls)==1 and calls[0].full_url==ENDPOINT


@pytest.mark.parametrize('mutation',[
    lambda p:p.update(model='unapproved/model'),
    lambda p:p.update(stream=True),
    lambda p:p.update(max_tokens=513),
    lambda p:p.update(extra_body={'secret':'bad'}),
    lambda p:p['provider'].update(allow_fallbacks=True),
    lambda p:p['provider'].update(zdr=False),
    lambda p:p['provider'].update(only=['Other']),
])
def test_writing_preflight_never_sends_invalid_requests(mutation):
    transport,calls=writer()
    payload=writing_payload();mutation(payload)
    with pytest.raises(PilotError):
        transport.send(endpoint=ENDPOINT,payload=payload,timeout_seconds=2,operation_id='op')
    assert calls==[]


@pytest.mark.parametrize('field,value',[('model','other/model'),('provider','Other'),('usage',{})])
def test_writing_reply_identity_and_accounting_fail_closed(field,value):
    body=writing_reply();body[field]=value
    transport,calls=writer(body)
    with pytest.raises(PilotError):
        transport.send(endpoint=ENDPOINT,payload=writing_payload(),timeout_seconds=2,operation_id='op')
    assert len(calls)==1


def test_live_catalog_exact_mapping_and_explicit_price_policy(monkeypatch):
    import live_writing
    model='anthropic/claude-sonnet-test'
    data={'data':{'id':model,'endpoints':[{'provider_name':'Anthropic','status':0,
        'context_length':1000,'max_completion_tokens':512,
        'pricing':{'prompt':'0.000003','completion':'0.000015'}}]}}
    class Opener:
        def open(self,url,timeout):
            assert url.endswith(model+'/endpoints')
            return io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr(live_writing,'_opener',lambda *args:Opener())
    catalog=OpenRouterWritingCatalog(https_proxy='http://127.0.0.1:8080',
        alias_targets={ALIASES['routine']:model},provider='Anthropic',privacy_check=lambda *args:True,
        cache_read_price='0.000003',cache_write_price='0.000006',request_price='0',clock=lambda:150.)
    target=catalog.resolve(model,alias=ALIASES['routine'],prompt='Synthetic',max_output_tokens=20)
    assert target.model==model and target.input_token_upper_bound==980
    assert target.request_price=='0' and target.expires_at==210.
    with pytest.raises(PilotError):
        catalog.resolve('anthropic/claude-sonnet-other',alias=ALIASES['routine'],prompt='Synthetic',max_output_tokens=20)
    catalog.privacy_check=lambda *args:False
    with pytest.raises(PilotError):
        catalog.resolve(model,alias=ALIASES['routine'],prompt='Synthetic',max_output_tokens=20)
