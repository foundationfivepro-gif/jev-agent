"""U04–U07 / R01: synthetic, hermetic host-contract and routing checks."""
from __future__ import annotations

import copy
import itertools
import socket
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from host_adapters import (ClaudeAdapter, DotAdapter, LocalCodexAdapter, ChatGPTAdapter,
                           TrustedJevSelector, dispatch_recommendation, recommend_route)
from host_contracts import CapabilitySnapshot, DecisionResponse, JevRouteDecision, TaskEnvelope
from routing_policy import jev_selection_scope
import model_router


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected network or live JEV call in synthetic host test")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(model_router, "decide", forbidden)
    monkeypatch.setattr(model_router, "write_trace", lambda *a, **kw: None)


def model(model_id="gpt-6.1-sol", family="sol", namespace="openai_native", **kwargs):
    return {"model_id": model_id, "namespace": namespace, "family": family,
            "efforts": ["low", "medium", "high"], "supported_tools": ["read_file"],
            "available": True, "cost_in": None, "cost_out": None, **kwargs}


def snapshot(**kwargs):
    return {"schema_version": "1.0", "surface": "dot_parent", "runtime_id": "runtime-1",
            "session_id": "session-1", "observed_at": 100.0, "expires_at": 200.0,
            "available": True, "evidence_status": "synthetic",
            "observed_tools": [{"name": "read_file", "input_schema": {"type": "object", "properties": {}}}],
            "dispatch_controls": ["inline", "delegate", "model_override", "effort_override"],
            "models": [model("gpt-6-luna", "luna"), model(), model("gpt-6-astra", "astra")],
            "current_model_id": "gpt-6.1-sol", "current_model_namespace": "openai_native",
            "current_effort": "medium", **kwargs}


def task(**kwargs):
    return {"schema_version": "1.0", "request_id": "request-1", "purpose": "Synthetic task",
            "acceptance_criteria": ["Fixture check passes"], "context_references": ["fixture/source.py"],
            "data_class": "public", "authorized_destinations": [], "budget_usd": None,
            "deadline": 180.0, "task_kind": "substantial", **kwargs}


def route(t=None, s=None, **kwargs):
    """Production defaults: no selector means no recommendation."""
    return recommend_route(t if t is not None else task(), s if s is not None else snapshot(),
                           runtime_id="runtime-1", session_id="session-1", now=150.0, **kwargs)


def jev_decision(proposal, *, request_id="request-1", runtime_id="runtime-1", session_id="session-1", **kwargs):
    return {"decision_id": "synthetic-jev-decision-1", "request_id": request_id,
            "runtime_id": runtime_id, "session_id": session_id, "request_fingerprint": "0" * 64,
            "candidate": proposal, "usage": {"input_tokens": 0, "output_tokens": 0},
            "cost_usd": 0.0, **kwargs}


def synthetic_selector(proposal=None, calls=None):
    """Explicit offline JEV fixture. These choices are not production defaults."""
    def select(*, task, runtime_id, session_id, request_fingerprint, eligible_models, execution_modes):
        if calls is not None:
            calls.append({"task": task, "runtime_id": runtime_id, "session_id": session_id,
                          "fingerprint": request_fingerprint, "models": eligible_models,
                          "modes": execution_modes})
        if proposal is not None:
            choice = proposal
        else:
            kind = "hard" if task.failed_acceptance_checks else task.task_kind
            wanted = {"narrow": "gpt-6-luna", "substantial": "gpt-6.1-sol", "hard": "gpt-6-astra"}[kind]
            wanted = task.explicit_model_id or task.compatibility_model_id or wanted
            selected = next((m for m in eligible_models if m.model_id == wanted), eligible_models[0])
            preferred = {"narrow": "low", "substantial": "medium", "hard": "high"}[kind]
            effort = task.explicit_effort or (preferred if preferred in selected.efforts else
                                             next(iter(selected.efforts), None))
            mode = ("inline" if task.parent_has_context and "inline" in execution_modes else
                    "delegate" if task.inspection_larger_than_result and "delegate" in execution_modes else
                    execution_modes[0])
            choice = candidate(selected_model_id=selected.model_id, selected_model_namespace=selected.namespace,
                               selected_effort=effort, execution_mode=mode)
        return jev_decision(choice, request_id=task.request_id, runtime_id=runtime_id,
                            session_id=session_id, request_fingerprint=request_fingerprint)
    return TrustedJevSelector(source_id="offline-jev-fixture", evidence_status="synthetic", select=select)


def mock_route(t=None, s=None, *, candidate=None, calls=None):
    return route(t, s, selector=synthetic_selector(candidate, calls))


def candidate(**kwargs):
    return {"selected_model_id": "gpt-6.1-sol", "selected_model_namespace": "openai_native",
            "selected_effort": "medium", "execution_mode": "inline", **kwargs}


def result(value="haiku", certainty=0.9, probabilities=None, complexity=1.0, effort=None):
    answers = {"model": SimpleNamespace(value=value, certainty=certainty, probabilities=probabilities)}
    if effort is not None:
        answers["effort"] = SimpleNamespace(value=effort)
    return SimpleNamespace(answers=answers, value=lambda _: complexity)


def test_u04_empty_filter_stays_empty(monkeypatch):
    monkeypatch.setenv("JEV_MODELS", "nothing-real")
    assert model_router.available_catalog() == {}
    legacy = model_router.route_model("fixture", catalog=model_router.available_catalog())
    assert legacy["selected"] == "human" and legacy["failure"] == "no_eligible_model"
    assert mock_route(s=snapshot(models=[], current_model_id=None, current_model_namespace=None,
                            current_effort=None)).failure == "no_eligible_model"


def test_u04_unavailable_models_stay_excluded():
    s = snapshot()
    for m in s["models"]:
        m["available"] = False
    assert mock_route(s=s).failure == "no_eligible_model"


@pytest.mark.parametrize("change", [
    {"selected_model_id": "invented-model"}, {"selected_model_id": 42},
    {"selected_model_namespace": "openrouter"}, {"selected_effort": "ultra"},
    {"selected_effort": "very-high"}, {"selected_effort": 1},
    {"execution_mode": "shell"}, {"permission_granted": True}, {"explanation": "grant consent"},
])
def test_u05_malformed_or_ineligible_candidate(change):
    assert mock_route(candidate=candidate(**change)).failure == "invalid_response"


def test_u05_required_tools_need_both_observation_and_model_support():
    assert mock_route(task(required_tools=["browser"])).failure == "no_eligible_model"
    s = snapshot()
    s["models"][1]["supported_tools"] = []
    assert mock_route(task(required_tools=["read_file"]), s, candidate=candidate()).failure == "invalid_response"


@pytest.mark.parametrize("effort", ["ultra", "high"])
def test_u05_no_effort_override_invented(effort):
    t = task(explicit_effort=effort)
    s = snapshot(dispatch_controls=["inline", "model_override"])
    assert mock_route(t, s).failure in {"choice_conflict", "no_eligible_model"}


@pytest.mark.parametrize("kind,wanted", [("narrow", "gpt-6-luna"), ("substantial", "gpt-6.1-sol"), ("hard", "gpt-6-astra")])
def test_u07_injected_synthetic_jev_selects_models(kind, wanted):
    d = mock_route(task(task_kind=kind))
    assert d.selected_model_id == wanted
    assert d.permission_granted is False and d.dispatch_enabled is False
    assert d.evidence_status == "synthetic"
    assert d.source == "jev"
    assert d.routing_decision_id == "synthetic-jev-decision-1"
    assert d.routing_source_id == "offline-jev-fixture"
    assert d.routing_evidence_status == "synthetic"
    assert "Validated JEV" in d.explanation


def test_u07_injected_jev_receives_acceptance_evidence():
    d = mock_route(task(failed_acceptance_checks=["Synthetic correctness assertion failed"]))
    assert d.selected_model_id == "gpt-6-astra"


@pytest.mark.parametrize("old", ["gpt-6-sol", "gpt-5.6-sol"])
def test_compatibility_requires_explicit_choice_or_reason(old):
    s = snapshot(models=[model(old)], current_model_id=old)
    assert mock_route(s=s).failure == "no_eligible_model"
    assert mock_route(task(explicit_model_id=old, explicit_model_namespace="openai_native"), s).selected_model_id == old
    assert mock_route(task(compatibility_model_id=old, compatibility_reason="Fixture host only exposes this version"), s).selected_model_id == old
    assert mock_route(task(compatibility_model_id=old), s).failure == "invalid_request"


def test_u06_explicit_model_and_family_win():
    assert mock_route(task(task_kind="narrow", explicit_model_id="gpt-6-astra",
                      explicit_model_namespace="openai_native")).selected_model_id == "gpt-6-astra"
    assert mock_route(task(explicit_model_family="luna")).selected_model_id == "gpt-6-luna"
    assert mock_route(task(explicit_model_family="not-installed")).failure == "choice_conflict"
    assert mock_route(task(explicit_model_id="gpt-6-luna")).failure == "invalid_request"


def test_u06_candidate_cannot_override_user_choice_or_effort():
    assert mock_route(task(explicit_model_family="luna"), candidate=candidate()).failure == "invalid_response"
    assert mock_route(task(explicit_effort="high"), candidate=candidate()).failure == "invalid_response"


def test_u06_host_restrictions_win_and_current_choice_preserved():
    s = snapshot(dispatch_controls=["inline"])
    assert mock_route(task(explicit_model_id="gpt-6-luna", explicit_model_namespace="openai_native"), s).failure == "choice_conflict"
    d = mock_route(s=s)
    assert d.selected_model_id == "gpt-6.1-sol" and d.selected_effort == "medium"
    assert mock_route(s=snapshot(dispatch_controls=[])).failure == "unavailable"


def test_inline_and_delegate_are_explicit_recommendations():
    assert mock_route().execution_mode == "inline"
    assert mock_route(task(parent_has_context=False, inspection_larger_than_result=True)).execution_mode == "delegate"
    assert mock_route(task(parent_has_context=True, inspection_larger_than_result=True)).execution_mode == "inline"
    d = dispatch_recommendation(mock_route(), live_dispatch_enabled=True, authorized=True)
    assert d.failure == "unavailable" and d.permission_granted is False


@pytest.mark.parametrize("time_change", [{"expires_at": 150.0}, {"observed_at": 151.0},
                                         {"available": False}, {"evidence_status": "unverified"}])
def test_expired_or_unverified_discovery_blocks(time_change):
    assert mock_route(s=snapshot(**time_change)).failure == "unavailable"


def test_session_discovery_does_not_propagate():
    s = snapshot()
    for adapter in [DotAdapter(runtime_id="child", session_id="session-1"),
                    DotAdapter(runtime_id="runtime-1", session_id="child"),
                    LocalCodexAdapter(runtime_id="runtime-1", session_id="session-1")]:
        assert adapter.recommend(task(), s, now=150.0).failure == "unavailable"
    assert mock_route(task(deadline=149.0)).failure == "unavailable"


@pytest.mark.parametrize("namespace,identifier", [("openai_api", "gpt-6.1-sol"), ("openrouter", "openai/gpt-6.1-sol"),
                                                 ("chatgpt_ui", "GPT-6.1-Sol")])
def test_native_namespace_cannot_be_substituted(namespace, identifier):
    s = snapshot(models=[model(identifier, namespace=namespace)], current_model_id=identifier,
                 current_model_namespace=namespace)
    assert mock_route(s=s).failure == "no_eligible_model"


def test_chatgpt_cannot_switch_ui_models():
    s = snapshot(surface="chatgpt_cloud", models=[model("GPT-Sol", namespace="chatgpt_ui")],
                 current_model_id="GPT-Sol", current_model_namespace="chatgpt_ui", dispatch_controls=["inline"])
    d = ChatGPTAdapter(runtime_id="runtime-1", session_id="session-1", selector=synthetic_selector()).recommend(task(), s, now=150.0)
    assert d.selected_model_id == "GPT-Sol" and d.execution_mode == "inline"
    assert not d.dispatch_enabled


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, "1", True, 10**400])
def test_nan_negative_or_coerced_costs_rejected(bad):
    s = snapshot()
    s["models"][0]["cost_in"] = bad
    assert mock_route(s=s).failure == "invalid_catalog"
    cat = copy.deepcopy(model_router.DEFAULT_CATALOG)
    cat["haiku"]["cost_out"] = bad
    assert model_router.route_model("fixture", catalog=cat)["failure"] == "invalid_catalog"
    with pytest.raises(ValueError):
        model_router.estimate_costs(cat, context_mtok=1.0, output_mtok=1.0, tool_mtok=1.0)


@pytest.mark.parametrize("model_change", [{"model_id": 4}, {"efforts": ["invented"]}, {"efforts": ["high", "high"]},
                                         {"family": "wrong"}, {"model_id": "openai/gpt-6.1-sol"}])
def test_bad_snapshot_models_rejected(model_change):
    s = snapshot()
    s["models"][1].update(model_change)
    assert mock_route(s=s).failure == "invalid_catalog"


@pytest.mark.parametrize("contract,payload", [(CapabilitySnapshot, snapshot()), (TaskEnvelope, task())])
def test_strict_version_and_extra_fields(contract, payload):
    with pytest.raises(ValidationError):
        contract.model_validate({**payload, "extra": "not allowed"})
    with pytest.raises(ValidationError):
        contract.model_validate({**payload, "schema_version": "2.0"})
    assert contract.model_validate(payload).to_dict()["schema_version"] == "1.0"


def test_recommendation_schema_cannot_grant_permission():
    d = mock_route().to_dict()
    for update in [{"permission_granted": True}, {"permission_granted": 0}, {"dispatch_enabled": True}, {"selected_model_id": None},
                   {"failure": "unavailable"}, {"extra": 1}]:
        with pytest.raises(ValidationError):
            DecisionResponse.model_validate({**d, **update})
    failed = mock_route(s=snapshot(dispatch_controls=[])).to_dict()
    with pytest.raises(ValidationError):
        DecisionResponse.model_validate({**failed, "selected_model_id": "gpt-6.1-sol"})


def test_mutation_is_revalidated_at_boundary():
    s = CapabilitySnapshot.model_validate(snapshot())
    s.models.append(s.models[0])
    assert mock_route(s=s).failure == "invalid_catalog"


def test_all_catalog_subsets_never_widen_choices():
    models = snapshot()["models"]
    for size in range(4):
        for subset in itertools.combinations(models, size):
            s = snapshot(models=list(subset), current_model_id=None, current_model_namespace=None, current_effort=None)
            for kind in ["narrow", "substantial", "hard"]:
                d = mock_route(task(task_kind=kind), s)
                if d.status == "recommended":
                    assert d.selected_model_id in {m["model_id"] for m in subset}
                else:
                    assert d.failure == "no_eligible_model"


@pytest.mark.parametrize("value,certainty,probs,complexity,effort", [
    ("unknown", 0.9, {}, 1.0, None), (42, 0.9, {}, 1.0, None),
    ("haiku", float("nan"), {}, 1.0, None), ("haiku", 0.9, {"haiku": float("nan")}, 1.0, None),
    ("haiku", 0.9, {"unknown": 1.0}, 1.0, None), ("haiku", 0.9, {"haiku": 0.1}, 1.0, None),
    ("haiku", 0.9, {"haiku": 0.1, "opus": 0.9}, 1.0, None),
    ("haiku", 0.9, {}, float("nan"), None), ("haiku", 0.9, {}, 1.0, "ultra"),
])
def test_legacy_malformed_decision_rejected(monkeypatch, value, certainty, probs, complexity, effort):
    monkeypatch.setattr(model_router, "decide", lambda *args: result(value, certainty, probs, complexity, effort))
    assert model_router.route_model("fixture")["failure"] == "invalid_response"


@pytest.mark.parametrize("value,certainty,expected", [("haiku", 0.9, "haiku"), ("sonnet", 0.9, "sonnet"),
                                                    ("fable", 0.9, "fable"), ("fable", 0.3, "opus")])
def test_r01_claude_golden_selections_unchanged(monkeypatch, value, certainty, expected):
    monkeypatch.setattr(model_router, "decide", lambda *args: result(value, certainty))
    assert model_router.route_model("fixture")["selected"] == expected
    assert set(model_router.DEFAULT_CATALOG) == {"haiku", "sonnet", "opus", "fable"}


def test_claude_adapter_keeps_executor_namespace():
    s = snapshot(surface="claude", models=[model("sonnet", "sonnet", "claude_executor")],
                 current_model_id="sonnet", current_model_namespace="claude_executor")
    d = ClaudeAdapter(runtime_id="runtime-1", session_id="session-1", selector=synthetic_selector()).recommend(task(), s, now=150.0)
    assert d.selected_model_id == "sonnet" and d.selected_model_namespace == "claude_executor"


def test_unknown_prices_do_not_claim_savings_and_token_overflow_rejected():
    assert mock_route().status == "recommended"  # routing only, with no paid operation
    assert model_router.estimate_costs({}, context_mtok=0.0, output_mtok=0.0, tool_mtok=0.0)["delegated"] is None
    with pytest.raises(ValueError, match="overflow"):
        model_router.estimate_costs(model_router.DEFAULT_CATALOG, context_mtok=1e308, output_mtok=1e308, tool_mtok=1e308)


@pytest.mark.parametrize("choices", [
    {}, {"explicit_model_id": "gpt-6.1-sol", "explicit_model_namespace": "openai_native"},
    {"explicit_model_family": "sol"}, {"explicit_effort": "medium"},
])
def test_missing_jev_never_silently_selects_even_explicit_choices(choices):
    d = route(task(**choices))
    assert d.status == "unavailable" and d.source == "unavailable"
    assert d.selected_model_id is None and d.routing_decision_id is None
    assert d.routing_source_id is None and d.routing_evidence_status is None
    assert not d.dispatch_enabled and not d.permission_granted


def test_bare_candidate_and_caller_attestations_are_not_trusted_bindings():
    for spoof in (candidate(), jev_decision(candidate()),
                  {"source_id": "jev", "evidence_status": "jev_observed", "candidate": candidate()},
                  lambda **kwargs: jev_decision(candidate())):
        assert route(selector=spoof).status == "unavailable"
    with pytest.raises(TypeError):
        route(candidate=candidate())
    d = route(task(routing_decision_id="caller-proof"))
    assert d.status == "invalid_request"


@pytest.mark.parametrize("update", [
    {"decision_id": ""}, {"decision_id": 42}, {"request_id": "another-request"},
    {"runtime_id": "another-runtime"}, {"session_id": "another-session"},
    {"routing_evidence_status": "jev_observed"}, {"routing_source_id": "caller-jev"},
    {"permission_granted": True}, {"source": "jev"},
])
def test_bound_jev_decision_is_strict_and_correlated(update):
    def select(**kwargs):
        return {**jev_decision(candidate(), request_fingerprint=kwargs["request_fingerprint"]), **update}
    binding = TrustedJevSelector("offline-jev-fixture", "synthetic", select)
    assert route(selector=binding).status == "invalid_response"


@pytest.mark.parametrize("task_change,snapshot_change,expected", [
    ({"extra": "invalid"}, {}, "invalid_request"),
    ({}, {"session_id": "another-session"}, "unavailable"),
    ({}, {"surface": "invented-host"}, "invalid_catalog"),
    ({}, {"expires_at": 149.0}, "unavailable"),
    ({"deadline": 149.0}, {}, "unavailable"),
    ({"required_tools": ["missing_tool"]}, {}, "no_eligible_model"),
    ({"explicit_model_family": "missing"}, {}, "choice_conflict"),
    ({}, {"dispatch_controls": []}, "unavailable"),
    ({"explicit_model_id": "gpt-6-luna", "explicit_model_namespace": "openai_native"},
     {"dispatch_controls": ["inline"]}, "choice_conflict"),
    ({"explicit_effort": "high"}, {"dispatch_controls": ["inline"]}, "choice_conflict"),
])
def test_preflight_rejection_never_calls_jev(task_change, snapshot_change, expected):
    calls = []
    d = mock_route(task(**task_change), snapshot(**snapshot_change), calls=calls)
    assert d.status == expected and calls == []


def test_explicit_choices_restrict_jev_input_and_still_record_one_decision():
    calls = []
    d = mock_route(task(explicit_model_id="gpt-6-astra", explicit_model_namespace="openai_native",
                        explicit_model_family="astra", explicit_effort="high"), calls=calls)
    assert d.status == "recommended" and len(calls) == 1
    assert [(m.namespace, m.model_id, m.efforts) for m in calls[0]["models"]] == [
        ("openai_native", "gpt-6-astra", ["high"])]
    assert d.routing_decision_id and d.routing_evidence_status == "synthetic"


def test_jev_input_has_no_unavailable_wrong_namespace_or_unsupported_tool_candidates():
    calls = []
    s = snapshot(models=[model(), model("gpt-6-astra", "astra", available=False),
                         model("gpt-6-luna", "luna", supported_tools=[]),
                         model("api-model", "api", namespace="openai_api")])
    d = mock_route(task(required_tools=["read_file"]), s, calls=calls)
    assert d.status == "recommended" and len(calls) == 1
    assert [(m.namespace, m.model_id) for m in calls[0]["models"]] == [("openai_native", "gpt-6.1-sol")]


def test_current_fixed_host_choice_reaches_jev_but_is_not_automatically_approved():
    calls = []
    s = snapshot(dispatch_controls=["inline"])
    d = mock_route(s=s, calls=calls)
    assert d.status == "recommended" and len(calls) == 1
    assert [m.model_id for m in calls[0]["models"]] == ["gpt-6.1-sol"]
    assert calls[0]["models"][0].efforts == ["medium"]
    assert calls[0]["modes"] == ("inline",)
    assert mock_route(s=s, candidate=candidate(selected_model_id="gpt-6-astra")).status == "invalid_response"
    assert mock_route(s=s, candidate=candidate(selected_effort="high")).status == "invalid_response"
    assert mock_route(s=s, candidate=candidate(execution_mode="delegate")).status == "invalid_response"
    assert route(s=s).status == "unavailable"


def test_model_is_selected_by_jev_without_deterministic_task_kind_fallback():
    # The synthetic selector intentionally disagrees with the old hard->Astra
    # policy. JEV's eligible choice is authoritative, including its effort/mode.
    d = mock_route(task(task_kind="hard", failed_acceptance_checks=["fixture"]),
                   candidate=candidate(selected_model_id="gpt-6-luna", selected_effort="low",
                                       execution_mode="delegate"))
    assert d.status == "recommended" and d.selected_model_id == "gpt-6-luna"
    assert d.selected_effort == "low" and d.execution_mode == "delegate"


@pytest.mark.parametrize("output", [None, RuntimeError("private-provider-error")])
def test_jev_unavailable_has_no_retry_no_fallback_and_no_error_leak(output):
    calls = []
    def select(**kwargs):
        calls.append(kwargs)
        if isinstance(output, Exception):
            raise output
        return output
    binding = TrustedJevSelector("offline-jev-fixture", "synthetic", select)
    d = route(selector=binding)
    assert d.status == "unavailable" and d.selected_model_id is None and len(calls) == 1
    assert "private-provider-error" not in str(d.to_dict())


def test_jev_provenance_is_from_binding_not_snapshot_or_candidate():
    d = mock_route(s=snapshot(evidence_status="runtime_observed"))
    assert d.evidence_status == "runtime_observed" and d.routing_evidence_status == "synthetic"
    assert mock_route(candidate=candidate(routing_evidence_status="jev_observed")).status == "invalid_response"
    for update in ({"source": "policy"}, {"source": "model"}, {"routing_decision_id": None},
                   {"routing_source_id": None}, {"routing_evidence_status": None}):
        with pytest.raises(ValidationError):
            DecisionResponse.model_validate({**d.to_dict(), **update})
    with pytest.raises(ValidationError):
        DecisionResponse.model_validate({**route().to_dict(), "routing_decision_id": "fake-proof"})


def test_mutating_selector_inputs_cannot_widen_host_or_user_constraints():
    def mutate(**kwargs):
        kwargs["task"].required_tools.clear()
        kwargs["eligible_models"][0].efforts.append("high")
        return jev_decision(candidate(selected_effort="high", execution_mode="delegate"),
                            request_fingerprint=kwargs["request_fingerprint"])
    binding = TrustedJevSelector("offline-jev-fixture", "synthetic", mutate)
    t, s = task(required_tools=["read_file"]), snapshot(dispatch_controls=["inline"])
    assert route(t, s, selector=binding).status == "invalid_response"
    assert t["required_tools"] == ["read_file"] and s["dispatch_controls"] == ["inline"]


def test_mutated_jev_contract_instance_is_revalidated():
    response = JevRouteDecision.model_validate(jev_decision(candidate()))
    object.__setattr__(response.candidate, "selected_effort", "invented")
    binding = TrustedJevSelector("offline-jev-fixture", "synthetic", lambda **kwargs: response)
    assert route(selector=binding).status == "invalid_response"


def test_adapter_cannot_take_per_request_selector_or_candidate_override():
    calls = []
    adapter = DotAdapter(runtime_id="runtime-1", session_id="session-1", selector=synthetic_selector(calls=calls))
    assert adapter.recommend(task(), snapshot(), now=150.0).status == "recommended"
    assert len(calls) == 1
    for override in ({"selector": synthetic_selector()}, {"candidate": candidate()}):
        with pytest.raises(TypeError):
            adapter.recommend(task(), snapshot(), now=150.0, **override)


def test_adapter_surface_and_session_preflight_precedes_bound_jev():
    calls = []
    binding = synthetic_selector(calls=calls)
    for adapter in (LocalCodexAdapter(runtime_id="runtime-1", session_id="session-1", selector=binding),
                    DotAdapter(runtime_id="runtime-1", session_id="child", selector=binding)):
        assert adapter.recommend(task(), snapshot(), now=150.0).status == "unavailable"
    assert calls == []


def test_jev_control_selection_does_not_recursively_route_itself():
    calls = []
    with jev_selection_scope():
        d = mock_route(calls=calls)
    assert d.status == "unavailable" and "Recursive JEV" in d.explanation and calls == []
    # A completed/failed selection does not poison later independent requests.
    assert mock_route(calls=calls).status == "recommended" and len(calls) == 1


@pytest.mark.parametrize("now", [True, "150", float("nan"), float("inf"), 10**400])
def test_invalid_clock_never_calls_jev(now):
    calls = []
    d = recommend_route(task(), snapshot(), runtime_id="runtime-1", session_id="session-1",
                        now=now, selector=synthetic_selector(calls=calls))
    assert d.status == "invalid_request" and calls == []


def test_direct_local_claim_cannot_bypass_model_jev_requirement():
    assert route(task(purpose="Requires direct local execution")).status == "unavailable"
    assert route(task(operation="local_test", uses_model=False)).status == "invalid_request"


@pytest.mark.parametrize("task_change,snapshot_change", [
    ({"purpose": "Changed synthetic purpose"}, {}),
    ({"acceptance_criteria": ["Different acceptance condition"]}, {}),
    ({"budget_usd": 1.0}, {}), ({"deadline": 179.0}, {}),
    ({"required_tools": ["read_file"]}, {}),
    ({"context_references": ["another/source.py"]}, {}),
    ({"explicit_model_family": "sol"}, {}),
    ({}, {"expires_at": 199.0}), ({}, {"observed_at": 101.0}),
    ({}, {"models": [model()]}),
])
def test_cached_jev_receipt_cannot_replay_after_task_or_snapshot_changes(task_change, snapshot_change):
    cached = []
    def select(**kwargs):
        if not cached:
            cached.append(jev_decision(candidate(), request_fingerprint=kwargs["request_fingerprint"]))
        return cached[0]
    binding = TrustedJevSelector("offline-jev-fixture", "synthetic", select)
    original = route(selector=binding)
    assert original.status == "recommended" and len(original.routing_request_fingerprint) == 64
    assert route(task(**task_change), snapshot(**snapshot_change), selector=binding).status == "invalid_response"


def test_fingerprint_is_canonical_and_records_full_observation_without_sending_schemas():
    calls = []
    t, s = task(), snapshot()
    first = mock_route(t, s, calls=calls)
    second = mock_route(dict(reversed(list(t.items()))), dict(reversed(list(s.items()))), calls=calls)
    assert first.routing_request_fingerprint == second.routing_request_fingerprint
    assert calls[0]["fingerprint"] == first.routing_request_fingerprint
    assert calls[0]["task"].context_references == []
    assert "snapshot" not in calls[0] and "input_schema" not in repr(calls[0])
    assert t["context_references"] == ["fixture/source.py"]


@pytest.mark.parametrize("evidence", ["synthetic", "runtime_observed"])
def test_live_binding_blocked_before_call_without_provisioned_authorization_and_budget_boundary(evidence):
    calls = []
    binding = TrustedJevSelector("unprovisioned-live-jev", "jev_observed", lambda **kwargs: calls.append(kwargs))
    d = route(task(budget_usd=100.0, authorized_destinations=["jev"]),
              snapshot(evidence_status=evidence), selector=binding)
    assert d.status == "unavailable" and calls == [] and d.routing_cost_usd is None
    assert not d.dispatch_enabled and not d.permission_granted


def test_routing_usage_and_cost_are_recorded_as_synthetic_or_unknown_never_inferred_zero():
    assert mock_route().routing_usage.input_tokens == 0
    assert mock_route().routing_cost_usd == 0.0 and mock_route().routing_cost_kind == "synthetic"
    def unknown(**kwargs):
        return jev_decision(candidate(), request_fingerprint=kwargs["request_fingerprint"], usage=None, cost_usd=None)
    d = route(selector=TrustedJevSelector("offline-jev-fixture", "synthetic", unknown))
    assert d.status == "recommended" and d.routing_usage is None
    assert d.routing_cost_usd is None and d.routing_cost_kind == "unknown"
    with pytest.raises(ValidationError):
        DecisionResponse.model_validate({**d.to_dict(), "routing_cost_kind": "synthetic"})
    with pytest.raises(ValidationError):
        DecisionResponse.model_validate({**d.to_dict(), "routing_cost_usd": 0.0})


@pytest.mark.parametrize("cost", [-1.0, float("nan"), float("inf"), True, "0"])
def test_malformed_routing_cost_is_rejected(cost):
    def select(**kwargs):
        return jev_decision(candidate(), request_fingerprint=kwargs["request_fingerprint"], cost_usd=cost)
    assert route(selector=TrustedJevSelector("offline-jev-fixture", "synthetic", select)).status == "invalid_response"


@pytest.mark.parametrize("usage", [{"input_tokens": -1, "output_tokens": 1},
                                    {"input_tokens": True, "output_tokens": 1},
                                    {"input_tokens": 1.0, "output_tokens": 1},
                                    {"input_tokens": 1},
                                    {"input_tokens": 1, "output_tokens": 1, "extra": 1}])
def test_malformed_routing_usage_is_rejected(usage):
    def select(**kwargs):
        return jev_decision(candidate(), request_fingerprint=kwargs["request_fingerprint"], usage=usage)
    assert route(selector=TrustedJevSelector("offline-jev-fixture", "synthetic", select)).status == "invalid_response"


def test_synthetic_routing_cost_cannot_exceed_task_budget():
    def select(**kwargs):
        return jev_decision(candidate(), request_fingerprint=kwargs["request_fingerprint"], cost_usd=1.0)
    binding = TrustedJevSelector("offline-jev-fixture", "synthetic", select)
    assert route(task(budget_usd=0.5), selector=binding).status == "budget_exceeded"
    assert route(task(budget_usd=1.0), selector=binding).status == "recommended"


@pytest.mark.parametrize("data_class", ["confidential", "sensitive", "secret"])
def test_sensitive_task_class_never_reaches_jev_callback(data_class):
    calls = []
    assert mock_route(task(data_class=data_class), calls=calls).status == "privacy_unavailable"
    assert calls == []


@pytest.mark.parametrize("field", ["purpose", "acceptance_criteria", "failed_acceptance_checks", "context_references",
                                   "request_id", "schema_description", "schema_key"])
def test_host_privacy_canaries_rejected_before_selector_and_not_echoed(field):
    canary = "sk-live-0123456789abcdefghijklmnopqrstuv"
    t, s, calls = task(), snapshot(), []
    if field == "schema_description":
        s["observed_tools"][0]["input_schema"]["description"] = canary
    elif field == "schema_key":
        s["observed_tools"][0]["input_schema"][canary] = "Synthetic value"
    elif field in {"acceptance_criteria", "failed_acceptance_checks", "context_references"}:
        t[field] = [canary]
    else:
        t[field] = canary
    d = mock_route(t, s, calls=calls)
    assert d.status == "privacy_unavailable" and calls == []
    assert canary not in repr(d.to_dict())


def test_personal_data_and_short_secret_schema_fields_do_not_reach_callback():
    calls = []
    assert mock_route(task(purpose="patient: synthetic-value"), calls=calls).status == "privacy_unavailable"
    s = snapshot()
    s["observed_tools"][0]["input_schema"]["authorization"] = "dummy"
    assert mock_route(s=s, calls=calls).status == "privacy_unavailable"
    assert calls == []


def test_callback_decision_id_cannot_leak_secret_shaped_metadata():
    canary = "sk-live-0123456789abcdefghijklmnopqrstuv"
    def select(**kwargs):
        return jev_decision(candidate(), request_fingerprint=kwargs["request_fingerprint"], decision_id=canary)
    d = route(selector=TrustedJevSelector("offline-jev-fixture", "synthetic", select))
    assert d.status == "invalid_response" and canary not in repr(d.to_dict())


@pytest.mark.parametrize("deadline,expires_at", [(160.0, 200.0), (180.0, 160.0)])
def test_slow_jev_callback_cannot_return_stale_recommendation(monkeypatch, deadline, expires_at):
    import host_adapters
    ticks = iter([100.0, 110.0])
    monkeypatch.setattr(host_adapters.time, "monotonic", lambda: next(ticks))
    calls = []
    d = mock_route(task(deadline=deadline), snapshot(expires_at=expires_at), calls=calls)
    assert d.status == "unavailable" and len(calls) == 1
    assert "outlived" in d.explanation and d.selected_model_id is None
