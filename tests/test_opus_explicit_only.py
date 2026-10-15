"""
Opus is explicit-only (owner rule 2026-10-15): the router, its fallbacks, the MCP
tools and placement never pick it; an explicit Opus runs only with
JEV_ALLOW_OPUS_SOL=ADAMS_OK in the live environment.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import model_router
from model_router import (DEFAULT_CATALOG, available_catalog, eligible_models, gate_explicit_model,
                          is_opus, opus_allowed, route_model)


def test_default_catalog_marks_opus_explicit_only():
    assert DEFAULT_CATALOG["opus"]["escalation_only"] is True
    assert DEFAULT_CATALOG["opus"]["id"].startswith("claude-opus")
    assert "opus" not in eligible_models(DEFAULT_CATALOG)
    assert set(eligible_models(DEFAULT_CATALOG)) == {"haiku", "sonnet"}


def test_opus_is_never_eligible_even_in_an_override_or_with_the_ok(monkeypatch):
    """A caller catalog without the flag, or under another name, still cannot auto-pick Opus."""
    monkeypatch.setenv("JEV_ALLOW_OPUS_SOL", "ADAMS_OK")
    override = {
        "sonnet": {k: v for k, v in DEFAULT_CATALOG["sonnet"].items()},
        "opus": {k: v for k, v in DEFAULT_CATALOG["opus"].items() if k != "escalation_only"},
        "big": {**DEFAULT_CATALOG["sonnet"], "id": "claude-opus-9", "tier": 5},
    }
    assert set(eligible_models(override)) == {"sonnet"}
    assert set(eligible_models(available_catalog(override))) == {"sonnet"}


def test_jev_models_opus_only_menu_has_no_auto_route(monkeypatch):
    monkeypatch.setenv("JEV_MODELS", "opus")
    assert eligible_models(available_catalog()) == {}


@pytest.mark.parametrize("value, certainty", [("opus", 0.99), ("opus+high", 0.99), ("opus", 0.3)])
def test_route_model_never_selects_opus(monkeypatch, value, certainty):
    class Answer:
        pass
    Answer.value, Answer.certainty, Answer.probabilities = value, certainty, {"opus": certainty}

    class Result:
        answers = {"model": Answer()}
        def value(self, _): return 3.0

    monkeypatch.setenv("JEV_ALLOW_OPUS_SOL", "ADAMS_OK")   # the OK is not an auto-pick licence
    monkeypatch.setattr(model_router, "write_trace", lambda *a, **k: None)
    seen = {}
    def fake_decide(state, questions):
        seen["candidates"] = set(state["candidates"])
        return Result()
    monkeypatch.setattr(model_router, "decide", fake_decide)
    d = route_model("re-architect the whole system")
    assert d["selected"] == "sonnet"
    assert "opus" not in seen["candidates"] and "fable" not in seen["candidates"]


def test_estimate_costs_benchmark_is_not_opus():
    est = model_router.estimate_costs(DEFAULT_CATALOG, context_mtok=.65, output_mtok=.12, tool_mtok=.23)
    # delegation is judged against Sonnet, the strongest auto tier
    assert est["delegation_wins"] == (est["delegated"] < est["pure"]["sonnet"])


@pytest.mark.parametrize("name", ["opus", "OPUS", " claude-opus-5-5 ", "claude-opus-4-1"])
def test_gate_explicit_opus(monkeypatch, name):
    monkeypatch.delenv("JEV_ALLOW_OPUS_SOL", raising=False)
    assert is_opus(name)
    model, why = gate_explicit_model(name)
    assert model == "sonnet" and "ADAMS_OK" in why
    monkeypatch.setenv("JEV_ALLOW_OPUS_SOL", "ok")
    assert gate_explicit_model(name)[0] == "sonnet"
    monkeypatch.setenv("JEV_ALLOW_OPUS_SOL", "ADAMS_OK")
    assert opus_allowed()
    assert gate_explicit_model(name) == (name, None)


@pytest.mark.parametrize("name", ["haiku", "sonnet", "claude-sonnet-5-5", "claude-haiku-4-5"])
def test_gate_keeps_other_explicit_models(monkeypatch, name):
    monkeypatch.delenv("JEV_ALLOW_OPUS_SOL", raising=False)
    assert not is_opus(name)
    assert gate_explicit_model(name) == (name, None)


@pytest.mark.parametrize("server", ["mcp_server", "remote_server"])
def test_mcp_route_never_returns_opus(monkeypatch, server):
    import importlib
    mod = importlib.import_module(server)
    monkeypatch.setattr(mod, "active_transport", lambda: None)
    monkeypatch.setenv("JEV_ALLOW_OPUS_SOL", "ADAMS_OK")
    assert mod._route_or_floor("x", dict(DEFAULT_CATALOG))["selected"] == "sonnet"
    only_opus = {"opus": {k: v for k, v in DEFAULT_CATALOG["opus"].items() if k != "escalation_only"}}
    with pytest.raises(ValueError, match="inline"):
        mod._route_or_floor("x", only_opus)


def test_placement_never_names_opus():
    import placement
    assert "opus" not in placement.MODEL_FOR.values()
    for c in (0.1, 1.2, 2.2, 2.99, 3.0):
        assert placement.place("x", complexity=c)["model"] != "opus"
