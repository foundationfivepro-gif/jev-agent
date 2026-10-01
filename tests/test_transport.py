"""Transport: OpenRouter first, then TypeSafe's API, then the legacy Vercel gateway."""

from __future__ import annotations

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core
from core import Choice, Noul, Score, TransportError, active_transport, decide


@pytest.fixture(autouse=True)
def _no_openrouter_key(monkeypatch):
    """A real .env may have filled OPENROUTER_API_KEY; tests below opt in to it explicitly."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch, reply):
    """Patch urlopen; return the list of (url, headers, body) it was called with."""
    seen = []

    def urlopen(req, timeout):
        seen.append((req.full_url, dict(req.header_items()), json.loads(req.data)))
        return _Resp(json.dumps(reply).encode())

    monkeypatch.setattr(core, "_open_decision_request", urlopen)
    return seen


QUESTIONS = {
    "urgent": Noul(instructions="Does this convey urgency?"),
    "team": Choice(instructions="Which team?", criteria={"billing": "Payments", "technical": "Bugs"}),
    "mood": Score(instructions="How frustrated?", criteria=["Calm", "Frustrated", "Very angry"]),
}

TYPESAFE_REPLY = {  # shapes copied from docs.typesafe.ai/api
    "model": "jev-1.13.0",
    "answers": {
        "urgent": {"type": "noul", "noul": 0.95},
        "team": {"type": "choice", "choice": "billing",
                 "probabilities": {"billing": 0.88, "technical": 0.12}, "confidence": 0.81},
        "mood": {"type": "score", "score": 1.05, "legend": {"0": "Calm", "1": "Frustrated", "2": "Very angry"},
                 "probabilities": {"0": 0.0, "1": 0.95, "2": 0.05}, "confidence": 0.92},
    },
    "usage": {"input_tokens": 318, "output_tokens": 34},
}


@pytest.mark.parametrize("keys, expected", [
    ({"TYPESAFE_API_KEY": "t"}, "typesafe"),
    ({"AI_GATEWAY_API_KEY": "g"}, "gateway"),
    ({"TYPESAFE_API_KEY": "t", "AI_GATEWAY_API_KEY": "g"}, "typesafe"),
    ({"OPENROUTER_API_KEY": "o", "TYPESAFE_API_KEY": "t", "AI_GATEWAY_API_KEY": "g"}, "openrouter"),
    ({"OPENROUTER_API_KEY": "o", "AI_GATEWAY_API_KEY": "g"}, "openrouter"),
    ({}, ""),
])
def test_key_order_openrouter_then_typesafe_then_gateway(monkeypatch, keys, expected):
    for var in ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "AI_GATEWAY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    for var, val in keys.items():
        monkeypatch.setenv(var, val)
    assert active_transport() == expected


def test_typesafe_request_and_answers_match_the_documented_api(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw-test")          # present but never used
    seen = _capture(monkeypatch, TYPESAFE_REPLY)

    d = decide({"ticket": "payouts failing"}, QUESTIONS)

    [(url, headers, body)] = seen
    assert url == "https://api.typesafe.ai/v1/systemone"
    assert headers["Authorization"] == "Bearer ts-test"
    assert body["model"] == "jev-latest"
    assert body["questions"]["urgent"]["type"] == "noul"
    assert d.transport == "typesafe" and d.model == "jev-1.13.0"
    assert d["urgent"] == 0.95 and d["team"] == "billing" and d["mood"] == 1.05
    assert d.certainty("team") == 0.81
    assert (d.input_tokens, d.output_tokens) == (318, 34)


def test_gateway_fallback_keeps_its_own_dialect(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw-test")
    seen = _capture(monkeypatch, {
        "answers": {"urgent": {"type": "boolean", "probability": 0.2}},
        "usage": {"inputTokens": 5, "outputTokens": 1},
    })
    d = decide("x", {"urgent": QUESTIONS["urgent"]})
    [(url, headers, body)] = seen
    assert url.endswith("/evaluate") and body["model"] == "typesafe-ai/jev"
    assert body["questions"]["urgent"]["type"] == "boolean"
    assert d.transport == "gateway" and d["urgent"] == 0.2 and d.input_tokens == 5


def test_typesafe_noul_outside_0_1_is_refused(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    _capture(monkeypatch, {"answers": {"urgent": {"type": "noul", "noul": 1.7}}, "usage": {}})
    with pytest.raises(core.InvalidResponse):
        decide("x", {"urgent": QUESTIONS["urgent"]})


def test_http_errors_name_the_transport(monkeypatch):
    import urllib.error

    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")

    def refuse(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key"}'))

    monkeypatch.setattr(core, "_open_decision_request", refuse)
    with pytest.raises(TransportError, match="typesafe 401"):
        decide("x", {"urgent": QUESTIONS["urgent"]})


def test_no_key_says_which_key_to_add(monkeypatch):
    for var in ("TYPESAFE_API_KEY", "AI_GATEWAY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(TransportError, match="OPENROUTER_API_KEY"):
        decide("x", {"urgent": QUESTIONS["urgent"]})


def test_openrouter_speaks_typesafe_dialect_at_its_documented_url(monkeypatch):
    """openrouter.ai/docs/guides/community/typesafe-sdk: same body, /api/v1/systemone."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")            # present but never used
    reply = {**TYPESAFE_REPLY, "id": "gen-dec-1", "provider": "TypeSafe",
             "model": "typesafe/jev-1.13-20260917",
             "usage": {**TYPESAFE_REPLY["usage"], "cost": 0.00003}}
    seen = _capture(monkeypatch, reply)

    d = decide({"ticket": "payouts failing"}, QUESTIONS)

    [(url, headers, body)] = seen
    assert url == "https://openrouter.ai/api/v1/systemone"
    assert headers["Authorization"] == "Bearer sk-or-test"
    assert body["model"] == "jev-latest" and body["questions"]["urgent"]["type"] == "noul"
    assert d.transport == "openrouter" and d.model == "typesafe/jev-1.13-20260917"
    assert d["urgent"] == 0.95 and d["team"] == "billing" and (d.input_tokens, d.output_tokens) == (318, 34)


def test_openrouter_criteria_only_noul_gets_neutral_instructions(monkeypatch):
    """OpenRouter 400s a noul without instructions; TypeSafe and the gateway never needed them."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    seen = _capture(monkeypatch, {"answers": {"ok": {"type": "noul", "noul": 0.3}}, "usage": {}})
    decide("x", {"ok": Noul(criteria={"true": "yes case", "false": "no case"})})
    sent = seen[0][2]["questions"]["ok"]
    assert sent["instructions"] == core.NOUL_BY_CRITERIA and sent["criteria"]["true"] == "yes case"

    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    seen = _capture(monkeypatch, {"answers": {"ok": {"type": "noul", "noul": 0.3}}, "usage": {}})
    decide("x", {"ok": Noul(criteria={"true": "yes case", "false": "no case"})})
    assert "instructions" not in seen[0][2]["questions"]["ok"]


def test_load_env_never_replaces_a_variable_already_set(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY='from-file'\nJEV_TEST_ONLY=x\n# comment\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    monkeypatch.delenv("JEV_TEST_ONLY", raising=False)
    core.load_env(env)
    assert os.environ["OPENROUTER_API_KEY"] == "from-env" and os.environ["JEV_TEST_ONLY"] == "x"
    monkeypatch.delenv("JEV_TEST_ONLY")
