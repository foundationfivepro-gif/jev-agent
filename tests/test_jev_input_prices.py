"""Public sparse tariff fixtures only; fake I/O and temporary test ledgers."""
import json
from decimal import Decimal
import pytest
from pydantic import ValidationError
from local_operator import JevInputOnlyPrices, build_operator, read_config, OperatorConfigError, normalize_arguments
from live_routing import LiveRoutingError, payload_fingerprint
from unified_mcp import create_server
from test_local_operator import fixture, FAKE
from test_unified_mcp import function

CANONICAL='typesafe/jev-1.13-20260917'


def prices():
    rate={'prompt':'0.000000042','completion':'0'}
    return dict(profile='jev_systemone_input_only',observed_at=149.,expires_at=195.,
        evidence='offline-public-catalog-fixture',alias='~typesafe/jev-latest',
        alias_target='typesafe/jev-1.13',canonical_model=CANONICAL,
        endpoint_model_id='typesafe/jev-1.13',provider_name='TypeSafe',
        supports_implicit_caching=False,context_tokens=32000,actual_models=[CANONICAL],
        alias_pricing=dict(rate),model_pricing=dict(rate),endpoint_pricing=dict(rate,discount=0))


def test_sparse_official_shape_and_full_context_bound():
    p=JevInputOnlyPrices.model_validate(prices())
    quote=p.quote({'model':'jev-latest','state':{},'questions':{}},150.,180.)
    assert Decimal(quote.ceiling_usd)==Decimal('.001344')
    assert quote.actual_models==frozenset({CANONICAL}) and quote.expires_at==180.


@pytest.mark.parametrize('change',['provider','alias','target','endpoint','canonical','actual','cache','context',
    'completion','prompt','discount','fee','override','missing_rate','rate_mismatch','fake_fee'])
def test_inconsistent_or_unsupported_metadata_rejected(change):
    p=prices()
    fields={'provider':('provider_name','Other'),'alias':('alias','jev-latest'),
        'target':('alias_target','typesafe/jev-1.14'),'endpoint':('endpoint_model_id','typesafe/jev-1.14'),
        'canonical':('canonical_model','typesafe/jev-1.14-20260917'),
        'actual':('actual_models',['typesafe/jev-1.13']),'cache':('supports_implicit_caching',True),
        'context':('context_tokens',64000),'fake_fee':('request_fee','0')}
    if change in fields:
        k,v=fields[change];p[k]=v
    elif change=='missing_rate':del p['model_pricing']['prompt']
    else:
        k,v={'completion':('completion','.1'),'prompt':('prompt','-1'),
             'discount':('discount',1),'fee':('request','0'),
             'override':('overrides',[]),'rate_mismatch':('prompt','.000000043')}[change]
        p['endpoint_pricing'][k]=v
    with pytest.raises(ValidationError):JevInputOnlyPrices.model_validate(p)


@pytest.mark.parametrize('change',['stale','expired','future','extension','model'])
def test_quote_freshness_and_request_shape(change):
    p=prices();now=150.;payload={'model':'jev-latest','state':{},'questions':{}}
    if change=='stale':
        now=500.;p['expires_at']=550.
    if change=='expired':p['expires_at']=150.
    if change=='future':p['observed_at']=151.
    if change=='extension':payload['plugins']=[]
    if change=='model':payload['model']='typesafe/jev-1.13'
    with pytest.raises(LiveRoutingError):JevInputOnlyPrices.model_validate(p).quote(payload,now,now+30.)


def setup(tmp_path, failure=None):
    file,c,request,args,calls,factory,ledger=fixture(tmp_path)
    c['prices']=prices();file.write_text(json.dumps(c))
    def transport_factory(provider,config):
        original=factory(provider,config)
        def send(payload,timeout):
            body=original(payload,timeout);body['model']=CANONICAL
            if failure=='model':body['model']='typesafe/jev-1.14-20261001'
            if failure=='provider':body['provider']='Other'
            if failure=='overbill':body['usage']['cost']=.001345
            if failure=='unknown':del body['usage']['cost']
            return body
        return send
    operator=build_operator(file,credential_provider=lambda:FAKE,transport_factory=transport_factory,clock=lambda:150.)
    return file,c,request,args,calls,ledger,create_server(operator)


def test_new_profile_routes_and_gates(tmp_path):
    _,_,request,args,calls,ledger,server=setup(tmp_path)
    assert function(server,'jev_recommend_host_route')(request)['status']=='recommended'
    assert function(server,'jev_gate_command')(**args)['source']=='model'
    assert len(calls)==2 and ledger.snapshot()['charged_usd']=='0.048548454'


@pytest.mark.parametrize('tool',['jev_recommend_host_route','jev_gate_command'])
@pytest.mark.parametrize('failure',['model','provider','overbill','unknown'])
def test_new_profile_halts_on_response_failure(tmp_path,tool,failure):
    _,_,request,args,calls,ledger,server=setup(tmp_path,failure)
    def invoke():
        fn=function(server,tool)
        return fn(request) if tool=='jev_recommend_host_route' else fn(**args)
    for _ in range(2):
        if tool=='jev_recommend_host_route':
            assert invoke()['status']=='unavailable'
        else:
            with pytest.raises(LiveRoutingError):invoke()
    assert len(calls)==1 and ledger.snapshot()['halted']


def test_context_each_subcall_must_fit_existing_operation_reservation(tmp_path):
    file,c,_,_,calls,ledger,server=setup(tmp_path)
    root=tmp_path/'public';root.mkdir()
    for n in range(26):(root/f'f{n}.py').write_text(f'def name_{n}(): pass')
    args=normalize_arguments('jev_select_context',{'goal':'Find public functions','root':str(tmp_path),'globs':['public/*.py']})
    c['operations']=[dict(tool='jev_select_context',arguments_sha256=payload_fingerprint(args),
        operation_id='bounded-context',ceiling_usd='.002',expires_at=195.,synthetic_attested=True,public_metadata_approved=True)]
    file.write_text(json.dumps(c))
    with pytest.raises(LiveRoutingError):function(server,'jev_select_context')(**args)
    # First actual charge .001 leaves .001; the next full-context .001344 quote cannot fit.
    assert len(calls)==1 and ledger.snapshot()['halted']
    assert ledger.snapshot()['charged_usd']=='0.047548454'


def test_profile_config_staleness_precedes_credentials(tmp_path):
    file,c,*_=fixture(tmp_path);c['prices']=prices();c['prices']['observed_at']=-151.
    file.write_text(json.dumps(c))
    with pytest.raises(OperatorConfigError):read_config(file,clock=lambda:150.)
