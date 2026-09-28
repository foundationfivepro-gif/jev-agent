"""Gates compare the chosen option's probability, whose meaning does not shift with menu size."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import model_router
import tool_router
from core import Answer, Decision, probability_bar


def test_probability_bar_matches_the_documented_confidence_formula():
    # docs.typesafe.ai/confidence: confidence ~ (n * peak - 1) / (n - 1)
    for n, conf in ((5, 0.75), (5, 0.5), (3, 0.75), (3, 0.70), (2, 0.8)):
        peak = probability_bar(conf, n)
        assert abs((n * peak - 1) / (n - 1) - conf) < 1e-9
    assert probability_bar(0.75, 5) == pytest.approx(0.80)
    assert probability_bar(0.75, 3) == pytest.approx(5 / 6)


def test_chosen_probability_reads_the_distribution():
    a = Answer("choice", "b", 0.3, {"a": 0.2, "b": 0.7, "c": 0.1})
    assert a.chosen_probability == 0.7 and a.certainty == 0.3
    assert Answer("choice", "b", 0.3, None).chosen_probability == 0.3     # no distribution: certainty
    assert Answer("noul", 0.9, None, None).chosen_probability == pytest.approx(0.8)


def _route(monkeypatch, value, probs, complexity=1.0, confidence=0.0):
    monkeypatch.setattr(model_router, "write_trace", lambda *a, **k: None)
    monkeypatch.setattr(model_router, "decide", lambda s, q: Decision({
        "model": Answer("choice", value, confidence, probs),
        "complexity": Answer("score", complexity, 0.9, None),
    }, "jev", "typesafe", 1, 1))


def test_a_shorter_menu_no_longer_raises_the_bar(monkeypatch):
    """JEV_MODELS=haiku,sonnet leaves 3 options; 81% on sonnet is a clear pick at any size."""
    monkeypatch.setenv("JEV_MODELS", "haiku,sonnet")
    probs = {"sonnet": 0.81, "haiku": 0.15, "human": 0.04}
    confidence = (3 * 0.81 - 1) / 2                     # 0.715: under the old 0.75 bar
    _route(monkeypatch, "sonnet", probs, confidence=confidence)
    d = model_router.route_model("add pagination", catalog=model_router.available_catalog())
    assert d["selected"] == "sonnet" and d["fallback"] is None and d["probability"] == 0.81


def test_below_the_bar_still_fails_toward_capability(monkeypatch):
    _route(monkeypatch, "haiku", {"haiku": 0.62, "sonnet": 0.30, "opus": 0.05, "fable": 0.02, "human": 0.01})
    d = model_router.route_model("x")
    assert d["selected"] == "sonnet" and d["fallback"] == "low_confidence"


def test_tool_router_gates_on_probability(monkeypatch):
    monkeypatch.setattr(tool_router, "write_trace", lambda *a, **k: None)
    for p, expected in ((0.86, "search_code"), (0.84, "none")):
        monkeypatch.setattr(tool_router, "decide", lambda s, q, p=p: Decision({
            "tool": Answer("choice", "search_code", 0.1, {"search_code": p, "run_tests": 1 - p, "none": 0.0}),
        }, "jev", "typesafe", 1, 1))
        assert tool_router.route_tool("find the auth tests", tool_router.DEMO_CATALOG)["selected"] == expected


def test_human_option_names_judgment_calls():
    """A decision a person must own should read as 'human', not as a cheap model's task."""
    import inspect
    assert "personnel, legal, financial or ethical" in inspect.getsource(model_router.route_model)


def test_human_as_top_answer_under_the_bar_stays_human(monkeypatch):
    """'Fire the vendor' came back human 0.78: under the bar, but not a reason to spend Opus."""
    _route(monkeypatch, "human", {"human": 0.78, "haiku": 0.17, "sonnet": 0.04, "opus": 0.01, "fable": 0.0})
    d = model_router.route_model("Decide whether we should fire the vendor")
    assert d["selected"] == "human" and d["fallback"] == "low_confidence_human"


def test_human_weight_on_a_hard_task_still_means_opus(monkeypatch):
    """Human as a minority signal of difficulty keeps the existing fail-toward-capability rule."""
    _route(monkeypatch, "sonnet", {"sonnet": 0.55, "human": 0.25, "opus": 0.2, "haiku": 0.0, "fable": 0.0})
    assert model_router.route_model("x")["selected"] == "opus"
