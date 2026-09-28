"""Transport: TypeSafe's API is preferred; the Vercel gateway is the legacy fallback."""

from __future__ import annotations

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core
from core import Choice, Noul, Score, TransportError, active_transport, decide


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

    monkeypatch.setattr(core.urllib.request, "urlopen", urlopen)
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
    ({}, ""),
])
def test_typesafe_key_wins(monkeypatch, keys, expected):
    for var in ("TYPESAFE_API_KEY", "AI_GATEWAY_API_KEY"):
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

    monkeypatch.setattr(core.urllib.request, "urlopen", refuse)
    with pytest.raises(TransportError, match="typesafe 401"):
        decide("x", {"urgent": QUESTIONS["urgent"]})


def test_no_key_says_which_key_to_add(monkeypatch):
    for var in ("TYPESAFE_API_KEY", "AI_GATEWAY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(TransportError, match="TYPESAFE_API_KEY"):
        decide("x", {"urgent": QUESTIONS["urgent"]})
