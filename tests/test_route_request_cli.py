import io
import json
from decimal import Decimal
import pytest
from scripts import route_request as cli
from live_pilot import Approval, PilotLedger
from host_contracts import TaskEnvelope
from test_host_adapters import task, snapshot


def setup(tmp_path):
    request=task(authorized_destinations=['openrouter.ai'],budget_usd=.1,context_references=[])
    request=TaskEnvelope.model_validate(request).model_dump(mode='json')
    auth={'approved_task_sha256':cli.payload_fingerprint(request),
          'snapshot':snapshot(evidence_status='runtime_observed'), 'expires_at':200.,
          'routing_ceiling_usd':'.01','actual_router_models':['typesafe/jev-1.13'],
          'pricing_evidence':'operator-verified-complete-price'}
    path=tmp_path/'ledger.sqlite'
    ledger=PilotLedger(path,Approval(cli.APPROVAL_ID,'5',frozenset({'TypeSafe'}),200.,jev_routing_approved=True))
    ledger.claim('previous','verified-charge',Decimal('.046548454'))
    ledger.finish('previous',{'status':'imported'},billed='.046548454')
    calls=[]
    def send(payload,timeout):
        calls.append(payload)
        return {'id':'gen-cli-test','model':'typesafe/jev-1.13','provider':'TypeSafe',
                'answers':{'route':{'type':'choice','choice':'0'}},
                'usage':{'input_tokens':3,'output_tokens':1,'cost':.001}}
    return request,auth,path,ledger,calls,send


def test_general_task_receipt_and_durable_replay(tmp_path):
    request,auth,path,ledger,calls,send=setup(tmp_path)
    result=cli.recommend(request,auth,transport=send,ledger_path=path,clock=lambda:150.)
    assert result['status']=='recommended'
    assert result['routing_evidence_status']=='jev_observed'
    assert cli.recommend(request,auth,transport=send,ledger_path=path,clock=lambda:150.)==result
    assert len(calls)==1
    assert ledger.snapshot()['charged_usd']=='0.047548454'
    assert calls[0]['model']=='jev-latest'


@pytest.mark.parametrize('change',['hash','expiry','private','future_catalog','missing_ledger'])
def test_invalid_authority_never_sends(tmp_path,change):
    request,auth,path,ledger,calls,send=setup(tmp_path)
    if change=='hash':request['purpose']='Changed task'
    if change=='expiry':auth['expires_at']=149.
    if change=='private':request['data_class']='internal';auth['approved_task_sha256']=cli.payload_fingerprint(request)
    if change=='future_catalog':auth['snapshot']['observed_at']=151.
    if change=='missing_ledger':path=tmp_path/'absent.sqlite'
    with pytest.raises(cli.LiveRoutingError):cli.recommend(request,auth,transport=send,ledger_path=path,clock=lambda:150.)
    assert not calls


def test_authority_file_requires_private_permissions_and_not_symlink(tmp_path):
    _,auth,*_=setup(tmp_path)
    file=tmp_path/'approval.json';file.write_text(json.dumps(auth));file.chmod(0o600)
    assert cli.read_authorization(file).pricing_evidence==auth['pricing_evidence']
    file.chmod(0o644)
    with pytest.raises(cli.LiveRoutingError):cli.read_authorization(file)
    link=tmp_path/'link';link.symlink_to(file)
    with pytest.raises(OSError):cli.read_authorization(link)


def test_cli_fingerprint_and_sanitized_errors(tmp_path,monkeypatch,capsys):
    request,*_=setup(tmp_path)
    monkeypatch.setattr(cli.sys,'stdin',io.StringIO(json.dumps(request)))
    assert cli.main(['--fingerprint'])==0
    assert json.loads(capsys.readouterr().out)['task_sha256']==cli.payload_fingerprint(request)
    monkeypatch.setattr(cli.sys,'stdin',io.StringIO('{"private":"secret",'))
    assert cli.main(['--fingerprint'])==1
    assert capsys.readouterr().out=='{"error":"live_routing_unavailable"}\n'


@pytest.mark.parametrize('raw',['{"a":1,"a":2}','{"a":NaN}',' '*65537])
def test_ambiguous_or_oversized_json_rejected(raw):
    with pytest.raises((ValueError,cli.LiveRoutingError)):cli.read_json(io.StringIO(raw))
