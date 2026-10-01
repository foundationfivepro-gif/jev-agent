"""Hermetic tests of the real live composition root, with HTTP replaced at I/O."""
from decimal import Decimal
import pytest
from host_contracts import TaskEnvelope
from live_routing import (BoundHostRouting, LiveRoutingError, OpenRouterSelector,
                          RoutingAuthorization, payload_fingerprint)
from live_pilot import Approval, PilotLedger
from test_host_adapters import task, snapshot


def setup(tmp_path, body=None):
    approval = Approval('routing-test', '5', frozenset({'TypeSafe'}), 999., jev_routing_approved=True)
    ledger = PilotLedger(tmp_path / 'canonical.sqlite', approval)
    ledger.claim('prior-paid-pilot', 'verified-prior-charge', Decimal('0.046511158'))
    ledger.finish('prior-paid-pilot', {'status': 'imported'}, billed='0.046511158')
    calls = []
    def send(payload, timeout):
        calls.append(payload)
        return body if body is not None else {
            'id': 'gen-test', 'model': 'typesafe/jev-1.13', 'provider': 'TypeSafe',
            'answers': {'route': {'type': 'choice', 'choice': '0'}},
            'usage': {'input_tokens': 42, 'output_tokens': 3, 'cost': 0.001}}
    selector = OpenRouterSelector(transport=send, ledger=ledger,
        authorize=lambda task, payload: RoutingAuthorization(payload_fingerprint(payload), '0.01',
            190., frozenset({'typesafe/jev-1.13'}), 'offline-pricing-fixture'), clock=lambda: 150.)
    host = BoundHostRouting(runtime_id='runtime-1', session_id='session-1',
        discover=lambda: snapshot(evidence_status='runtime_observed'), selector=selector.binding(),
        authorize=lambda task: True, clock=lambda: 150.)
    request = task(authorized_destinations=['openrouter.ai'], budget_usd=0.1)
    return host, selector, request, calls, ledger


def test_live_receipt_and_canonical_budget(tmp_path):
    host, selector, request, calls, ledger = setup(tmp_path)
    receipt = host.recommend(request)
    assert receipt.status == 'recommended'
    assert receipt.routing_evidence_status == 'jev_observed'
    assert receipt.routing_cost_kind == 'billed'
    assert receipt.routing_cost_usd == .001
    assert calls[0]['model'] == 'jev-latest'
    assert 'context_references' not in calls[0]['state']
    assert ledger.snapshot()['charged_usd'] == '0.047511158'
    assert host.recommend(request) == receipt
    assert len(calls) == 1


@pytest.mark.parametrize('changes', [dict(data_class='internal'), dict(authorized_destinations=[]),
    dict(budget_usd=None), dict(budget_usd=.001), dict(deadline=140.)])
def test_no_send_without_consent_budget_deadline(tmp_path, changes):
    host, _, request, calls, _ = setup(tmp_path)
    assert host.recommend({**request, **changes}).status != 'recommended'
    assert calls == []


@pytest.mark.parametrize('body', [{}, {'usage': {'cost': .001}},
    {'id': 'gen-test', 'model': 'other/model', 'provider': 'TypeSafe',
     'answers': {'route': {'type': 'choice', 'choice': '0'}},
     'usage': {'input_tokens': 1, 'output_tokens': 1, 'cost': .001}}])
def test_bad_receipt_halts_and_never_retries(tmp_path, body):
    host, _, request, calls, ledger = setup(tmp_path, body)
    assert host.recommend(request).status != 'recommended'
    assert host.recommend(request).status != 'recommended'
    assert len(calls) == 1
    assert ledger.snapshot()['halted']


def test_host_dispatch_only_after_validated_receipt_and_once(tmp_path):
    host, _, request, calls, _ = setup(tmp_path)
    dispatched = []
    host.dispatch = lambda **kwargs: dispatched.append(kwargs) or 'host-result'
    assert host.run(request) == 'host-result'
    assert dispatched[0]['decision'].routing_decision_id == 'gen-test'
    with pytest.raises(LiveRoutingError):
        host.run(request)
    assert len(dispatched) == len(calls) == 1


def test_changed_discovery_blocks_dispatch(tmp_path):
    host, _, request, _, _ = setup(tmp_path)
    observations = iter([snapshot(evidence_status='runtime_observed'),
                         snapshot(evidence_status='runtime_observed', expires_at=199.)])
    host.discover = lambda: next(observations)
    host.dispatch = lambda **kwargs: pytest.fail('changed catalog dispatched')
    with pytest.raises(LiveRoutingError):
        host.run(request)


def test_unbound_dispatch_is_unavailable_before_paid_call(tmp_path):
    host, _, request, calls, _ = setup(tmp_path)
    with pytest.raises(LiveRoutingError):
        host.run(request)
    assert calls == []


def test_identity_and_provenance_are_host_owned(tmp_path):
    host, _, request, calls, _ = setup(tmp_path)
    host.runtime_id = 'wrong-runtime'
    assert host.recommend(request).status == 'unavailable'
    assert calls == []
    host.discover = lambda: snapshot()
    with pytest.raises(LiveRoutingError):
        host.recommend(request)


def test_synthetic_receipt_cannot_dispatch_live(tmp_path):
    from test_host_adapters import synthetic_selector
    host, _, request, calls, _ = setup(tmp_path)
    host.selector = synthetic_selector()
    host.dispatch = lambda **kwargs: pytest.fail('synthetic receipt dispatched')
    with pytest.raises(LiveRoutingError):
        host.run(request)
    assert calls == []


def test_unverified_cost_allowance_is_not_authorization(tmp_path):
    host, selector, request, calls, _ = setup(tmp_path)
    selector.authorize = lambda task, payload: '0.01'
    assert host.recommend(request).status == 'unavailable'
    assert calls == []


def test_expired_price_evidence_blocks_before_io(tmp_path):
    host, selector, request, calls, _ = setup(tmp_path)
    selector.authorize = lambda task, payload: RoutingAuthorization(payload_fingerprint(payload),
        '0.01', 140., frozenset({'typesafe/jev-1.13'}), 'offline-price')
    assert host.recommend(request).status == 'unavailable'
    assert calls == []


def test_recursive_selection_never_sends(tmp_path):
    from routing_policy import jev_selection_scope
    host, _, request, calls, _ = setup(tmp_path)
    with jev_selection_scope():
        assert host.recommend(request).status == 'unavailable'
    assert calls == []


def test_actual_mcp_registration_uses_bound_catalog_not_tool_observation(tmp_path):
    import asyncio
    from live_routing import create_routing_server
    host, _, request, calls, _ = setup(tmp_path)
    server = create_routing_server(host)
    async def run():
        result = await server.call_tool('jev_recommend_host_route', {
            'task': request, 'snapshot': snapshot(runtime_id='forged-runtime'),
            'runtime_id': 'runtime-1', 'session_id': 'session-1'})
        # Exercise the actual SDK tool path; the forged snapshot is ignored.
        import json
        text = json.dumps(result, default=str)
        assert 'gen-test' in text and 'jev_observed' in text
    asyncio.run(run())
    assert len(calls) == 1


def test_http_transport_fixed_endpoint_bounded_and_sanitized():
    from live_routing import ProtectedOpenRouterTransport
    transport = ProtectedOpenRouterTransport(issued_placeholder=lambda: 'issued-test-placeholder',
                                             https_proxy='http://127.0.0.1:8080')
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, timeout))
            raise RuntimeError('private upstream error must never escape')
    transport._opener = Opener()
    with pytest.raises(LiveRoutingError) as error:
        transport({'model': 'jev-latest'}, 1.)
    assert str(error.value) == 'live_routing_unavailable'
    assert calls == [('https://openrouter.ai/api/v1/systemone', 1.)]
    with pytest.raises(LiveRoutingError):
        transport({'oversize': 'x' * 32769}, 1.)
    assert len(calls) == 1


def test_over_ceiling_charge_halts_dispatch(tmp_path):
    body = {'id': 'gen-test', 'model': 'typesafe/jev-1.13', 'provider': 'TypeSafe',
            'answers': {'route': {'type': 'choice', 'choice': '0'}},
            'usage': {'input_tokens': 42, 'output_tokens': 3, 'cost': .02}}
    host, _, request, calls, ledger = setup(tmp_path, body)
    host.dispatch = lambda **kwargs: pytest.fail('over-budget decision dispatched')
    with pytest.raises(LiveRoutingError):
        host.run(request)
    assert ledger.snapshot()['halted'] == 'charge_exceeded_reservation'


def test_shared_budget_exhaustion_prevents_send(tmp_path):
    host, _, request, calls, ledger = setup(tmp_path)
    ledger.claim('other-worker', 'other', Decimal('4.95'))
    ledger.finish('other-worker', {'status': 'settled'}, billed='4.95')
    assert host.recommend(request).status == 'unavailable'
    assert calls == []
    assert ledger.snapshot()['charged_usd'] == '4.996511158'


def test_cached_model_must_still_be_authorized(tmp_path):
    host, selector, request, calls, _ = setup(tmp_path)
    assert host.recommend(request).status == 'recommended'
    selector.authorize = lambda task, payload: RoutingAuthorization(payload_fingerprint(payload),
        '0.01', 190., frozenset({'typesafe/jev-new'}), 'updated-price')
    assert host.recommend(request).status == 'unavailable'
    assert len(calls) == 1


def test_protected_proxy_cannot_be_bypassed(monkeypatch):
    import urllib.request
    from live_routing import _ForcedProxy, ENDPOINT
    monkeypatch.setenv('NO_PROXY', '*')
    monkeypatch.setenv('no_proxy', '*')
    request = urllib.request.Request(ENDPOINT, data=b'{}')
    proxy = _ForcedProxy({'https': 'http://127.0.0.1:8080'})
    proxy.proxy_open(request, 'http://127.0.0.1:8080', 'https')
    assert request.host == '127.0.0.1:8080'
    assert request._tunnel_host == 'openrouter.ai'
