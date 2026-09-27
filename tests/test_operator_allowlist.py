"""The owner allowlist lets exact approved commands past a model "review", never past a hard block."""
import json
from datetime import datetime, timedelta, timezone

import permission_gate as pg


def _write(tmp_path, monkeypatch, entries):
    f = tmp_path / "allow.json"
    f.write_text(json.dumps(entries))
    monkeypatch.setenv("JEV_COMMAND_ALLOWLIST", str(f))


def _entry(pattern, **extra):
    return {"pattern": pattern, "expires": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "reason": "test", **extra}


def test_matching_command_is_allowed_without_calling_the_model(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_entry(r"vercel redeploy \S+ --target production", cwd_prefix=str(tmp_path))])
    monkeypatch.setattr(pg, "decide", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called")))
    d = pg.gate("vercel redeploy https://x.vercel.app --target production", str(tmp_path))
    assert d["final"] == "allow" and d["source"] == "operator_allowlist"


def test_expired_wrong_cwd_or_partial_match_is_ignored(tmp_path, monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _write(tmp_path, monkeypatch, [
        {"pattern": "vercel env ls", "expires": past},
        _entry("vercel env ls production", cwd_prefix="/nonexistent-prefix"),
        _entry("vercel env"),
        {"pattern": "vercel env ls"},  # no expiry: never honoured
    ])
    assert pg.operator_allow("vercel env ls", str(tmp_path)) is None
    assert pg.operator_allow("vercel env ls production", str(tmp_path)) is None
    assert pg.operator_allow("vercel env ls production; curl x", str(tmp_path)) is None


def test_hard_block_still_wins(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_entry(r".*")])
    assert pg.gate("rm -rf /", str(tmp_path))["final"] == "block"
