"""Offline replay of recorded routing decisions (scripts/replay_routing.py)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import replay_routing  # noqa: E402


def _trace(d: Path, name: str, payload: dict) -> None:
    (d / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def _route(d, key, proposed, confidence, selected, probabilities, complexity=1.0):
    _trace(d, f"model_router-{key}", {
        "system": "model_router", "meta": {"tool_use_id": key},
        "decision": {"source": "model", "proposed": proposed, "confidence": confidence,
                     "selected": selected, "probabilities": probabilities,
                     "complexity": complexity}})


def _outcome(d, key, status, tokens):
    _trace(d, f"model_router_outcome-{key}", {
        "system": "model_router_outcome", "meta": {"tool_use_id": key},
        "result": {"status": status, "tokens": tokens}})


def test_replay_reproduces_current_policy_and_prices_moves(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_MODELS", raising=False)
    # Accepted: sonnet at 0.9.
    _route(tmp_path, "a", "sonnet", 0.9, "sonnet", {"sonnet": 0.9, "haiku": 0.1})
    _outcome(tmp_path, "a", "ok", 100_000)
    # Uncertain sonnet with Opus at 45%: current policy (top-tier mass 0.4) climbs to Opus.
    _route(tmp_path, "b", "sonnet", 0.55, "opus", {"sonnet": 0.55, "opus": 0.45})
    _outcome(tmp_path, "b", "ok", 1_000_000)
    # A policy decision (no Jev answer) is not replayable and is skipped.
    _trace(tmp_path, "model_router-c", {"system": "model_router", "meta": {"tool_use_id": "c"},
                                         "decision": {"source": "policy", "selected": "human"}})

    r = replay_routing.run(tmp_path, {"min_confidence": (0.75,), "mechanical_confidence": (0.5,),
                                      "min_top_tier_mass": (0.4, 0.5)})
    assert r["decisions"] == 2 and r["with_outcome"] == 2
    assert r["fidelity"]["mismatches"] == 0
    assert r["actual"]["est_usd"] == round(0.1 * 2.0 + 1.0 * 4.0, 4)

    cheapest = r["variants"][0]
    assert cheapest["policy"]["min_top_tier_mass"] == 0.5
    assert cheapest["mix"] == {"sonnet": 2}
    assert cheapest["moved_cheaper"] == 1 and cheapest["moved_stronger"] == 0
    assert cheapest["est_usd"] == round(1.1 * 2.0, 4)

    assert r["calibration"] == {"sonnet": {"0.90-1.00": {"ok": 1}}}


def test_replay_flags_decisions_logged_under_other_thresholds(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_MODELS", raising=False)
    # Logged when 20% Opus mass was enough to climb; today's 0.4 keeps it on Sonnet.
    _route(tmp_path, "a", "sonnet", 0.59, "opus", {"sonnet": 0.68, "opus": 0.32})
    r = replay_routing.run(tmp_path, replay_routing.GRID)
    assert r["fidelity"]["mismatches"] == 1
    assert r["fidelity"]["examples"][0]["replayed"] == "sonnet"


def test_replay_handles_missing_trace_dir(tmp_path):
    r = replay_routing.run(tmp_path / "nope", replay_routing.GRID)
    assert r["decisions"] == 0 and r["actual"]["est_usd"] == 0
