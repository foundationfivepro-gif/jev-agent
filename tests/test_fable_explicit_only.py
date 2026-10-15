"""
Fable gets the same gate as Opus (Adam via Groktard, 2026-10-15): escalation_only,
never auto-picked, and an explicit Fable runs only with JEV_ALLOW_OPUS_SOL=ADAMS_OK
in the live environment; otherwise Sonnet.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import model_router
from model_router import (DEFAULT_CATALOG, eligible_models, gate_explicit_model, is_fable,
                          needs_ok)


def test_catalog_marks_fable_explicit_only_with_ok():
    spec = DEFAULT_CATALOG["fable"]
    assert spec["escalation_only"] is True and spec["requires_ok"] is True
    assert "fable" not in eligible_models(DEFAULT_CATALOG)


def test_fable_never_eligible_even_in_an_override(monkeypatch):
    monkeypatch.setenv("JEV_ALLOW_OPUS_SOL", "ADAMS_OK")
    override = {
        "sonnet": dict(DEFAULT_CATALOG["sonnet"]),
        "fable": {k: v for k, v in DEFAULT_CATALOG["fable"].items() if k != "escalation_only"},
        "big": {**DEFAULT_CATALOG["sonnet"], "id": "claude-fable-9", "tier": 6},
    }
    assert set(eligible_models(override)) == {"sonnet"}


@pytest.mark.parametrize("name", ["fable", "FABLE", " claude-fable-5-1 ", "claude-fable-6"])
def test_gate_explicit_fable(monkeypatch, name):
    monkeypatch.delenv("JEV_ALLOW_OPUS_SOL", raising=False)
    assert is_fable(name) and needs_ok(name)
    model, why = gate_explicit_model(name)
    assert model == "sonnet" and why.startswith("fable needs JEV_ALLOW_OPUS_SOL=ADAMS_OK")
    monkeypatch.setenv("JEV_ALLOW_OPUS_SOL", "yes")
    assert gate_explicit_model(name)[0] == "sonnet"
    monkeypatch.setenv("JEV_ALLOW_OPUS_SOL", "ADAMS_OK")
    assert gate_explicit_model(name) == (name, None)


def test_route_model_never_selects_fable(monkeypatch):
    class Answer:
        value, certainty, probabilities = "fable+max", 0.99, {"fable": 0.99}

    class Result:
        answers = {"model": Answer()}
        def value(self, _): return 3.0

    monkeypatch.setenv("JEV_ALLOW_OPUS_SOL", "ADAMS_OK")
    monkeypatch.setattr(model_router, "write_trace", lambda *a, **k: None)
    monkeypatch.setattr(model_router, "decide", lambda s, q: Result())
    assert model_router.route_model("finish the whole deck")["selected"] == "sonnet"


def _hook(payload, env_extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENROUTER_API_KEY", "AI_GATEWAY_API_KEY", "TYPESAFE_API_KEY",
                        "JEV_MODELS", "JEV_ALLOW_OPUS_SOL")}
    env["JEV_ENV_FILE"] = "/nonexistent/jev.env"
    env.update(env_extra)
    return subprocess.run([sys.executable, os.path.join(ROOT, "hooks.py"), "route-agent"],
                          input=json.dumps(payload), capture_output=True, text=True, env=env,
                          cwd=ROOT, timeout=60)


@pytest.mark.parametrize("named", ["fable", "claude-fable-5-1"])
def test_hook_explicit_fable_needs_adams_ok(named):
    spawn = {"tool_input": {"prompt": "write the report", "model": named}}
    out = json.loads(_hook(spawn, {}).stdout)["hookSpecificOutput"]
    assert out["updatedInput"]["model"] == "sonnet"
    assert "fable needs JEV_ALLOW_OPUS_SOL=ADAMS_OK" in out["permissionDecisionReason"]
    kept = json.loads(_hook(spawn, {"JEV_ALLOW_OPUS_SOL": "ADAMS_OK"}).stdout)["hookSpecificOutput"]
    assert kept["updatedInput"]["model"] == named


def test_env_file_cannot_grant_fable(tmp_path):
    envfile = tmp_path / "jev.env"
    envfile.write_text("JEV_ALLOW_OPUS_SOL=ADAMS_OK\n")
    r = _hook({"tool_input": {"prompt": "x", "model": "fable"}}, {"JEV_ENV_FILE": str(envfile)})
    assert json.loads(r.stdout)["hookSpecificOutput"]["updatedInput"]["model"] == "sonnet"


def _cloud(payload, env_extra, tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENROUTER_API_KEY", "JEV_MODELS", "JEV_ALLOW_OPUS_SOL")}
    env.update({"CLAUDE_CODE_REMOTE": "true", "JEV_AGENT_DIR": str(tmp_path / "absent"), **env_extra})
    return subprocess.run(["bash", os.path.join(ROOT, "scripts", "cloud-repo.sh"), "route-agent"],
                          input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60)


def test_cloud_floor_gates_fable(tmp_path):
    spawn = {"tool_input": {"prompt": "x", "model": "fable"}}
    out = json.loads(_cloud(spawn, {}, tmp_path).stdout)
    assert out["hookSpecificOutput"]["updatedInput"]["model"] == "sonnet"
    assert _cloud(spawn, {"JEV_ALLOW_OPUS_SOL": "ADAMS_OK"}, tmp_path).stdout == ""
