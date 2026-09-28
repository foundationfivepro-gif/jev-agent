"""Skill router: catalog from disk, the bar, "none", the deadline, grouped rounds, hook and switch."""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core
import skill_router
from core import Answer, Decision, TransportError


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setattr(skill_router, "STATE_FILE", tmp_path / "skill-router.json")
    monkeypatch.delenv("JEV_SKILL_ROUTER", raising=False)


def _choice(value, confidence, probs=None):
    return Answer("choice", value, confidence, probs or {value: confidence})


def _fake(monkeypatch, answer_for):
    """answer_for(qid, criteria) -> (value, confidence). Records every call."""
    calls = []

    def decide(state, questions):
        calls.append({qid: dict(q.criteria) for qid, q in questions.items()})
        return Decision({qid: _choice(*answer_for(qid, dict(q.criteria))) for qid, q in questions.items()},
                        "jev", "gateway", 10, 1)

    monkeypatch.setattr(skill_router, "decide", decide)
    return calls


SKILLS = {
    "docx": "Create or edit Word documents (.docx).",
    "xlsx": "Create or edit spreadsheets (.xlsx, .csv).",
    "pdf": "Read, merge or fill PDF files.",
}


# ------------------------------------------------------------------ catalog

def _skill(root, name, description, folder=None):
    d = root / (folder or name)
    d.mkdir(parents=True)
    fm = f"---\nname: {name}\n" + (f"description: {description}\n" if description else "") + "---\n\n# body\n"
    (d / "SKILL.md").write_text(fm)


def test_catalog_reads_user_nested_project_and_enabled_plugin_skills(tmp_path):
    home, proj = tmp_path / "home", tmp_path / "proj"
    _skill(home / ".claude" / "skills", "alpha", "Alpha: does a thing, with a colon")
    _skill(home / ".claude" / "skills", "synced-one", "Account-synced skill", folder="synced/bucket/synced-one")
    _skill(home / ".claude" / "skills", "nodesc", None)
    _skill(proj / "skills", "beta", "Project skill")
    _skill(proj / ".claude" / "skills", "alpha", "Project override of alpha")
    for plugin, on in (("good", True), ("off", False)):
        _skill(tmp_path / "plugins" / plugin / "skills", "tool", f"{plugin} plugin skill")
    (home / ".claude" / "plugins").mkdir(parents=True)
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        "good@mkt": [{"installPath": str(tmp_path / "plugins" / "good")}],
        "off@mkt": [{"installPath": str(tmp_path / "plugins" / "off")}],
    }}))
    (home / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": {"good@mkt": True, "off@mkt": False}}))

    cat = skill_router.build_catalog(proj, home=home)
    assert set(cat) == {"alpha", "synced-one", "beta", "good:tool"}
    assert cat["alpha"]["description"] == "Project override of alpha"
    assert cat["good:tool"]["description"] == "good plugin skill"


def test_catalog_is_reread_so_a_new_skill_shows_up(tmp_path):
    home = tmp_path / "home"
    _skill(home / ".claude" / "skills", "one", "first")
    assert set(skill_router.build_catalog(home=home)) == {"one"}
    _skill(home / ".claude" / "skills", "two", "second")
    assert set(skill_router.build_catalog(home=home)) == {"one", "two"}


# ------------------------------------------------------------------ routing

def test_picks_above_the_bar_and_offers_none(monkeypatch):
    calls = _fake(monkeypatch, lambda qid, c: ("xlsx", 0.82))
    d = skill_router.route_skill("turn this csv into a pivot table", SKILLS)
    assert d["selected"] == "xlsx" and d["source"] == "model" and d["rounds"] == 1
    assert "none" in calls[0]["g0"]
    assert skill_router.note(d) == ("jev: skill `xlsx` (82% sure) fits this request; "
                                    "load it unless it clearly does not.")


@pytest.mark.parametrize("value, confidence, reason", [
    ("xlsx", 0.55, "below the bar"),
    ("none", 0.90, "none of these"),
    ("made-up-skill", 0.95, "not in the skill list"),
])
def test_everything_else_is_none(monkeypatch, value, confidence, reason):
    _fake(monkeypatch, lambda qid, c: (value, confidence))
    d = skill_router.route_skill("do something", SKILLS)
    assert d["selected"] == "none" and d["reason"] == reason
    assert skill_router.note(d) is None


def test_slow_jev_is_none_within_the_deadline(monkeypatch):
    def slow(state, questions):
        time.sleep(3)
        raise AssertionError("never awaited")

    monkeypatch.setattr(skill_router, "decide", slow)
    started = time.monotonic()
    d = skill_router.route_skill("write a memo", SKILLS, deadline_ms=100)
    assert time.monotonic() - started < 1
    assert d["selected"] == "none" and d["source"] == "timeout"


@pytest.mark.parametrize("exc", [TransportError("gateway 503"), ValueError("bad answer")])
def test_errors_are_none_never_raised(monkeypatch, exc):
    def boom(state, questions):
        raise exc

    monkeypatch.setattr(skill_router, "decide", boom)
    d = skill_router.route_skill("write a memo", SKILLS)
    assert d["selected"] == "none" and d["source"] == "unavailable"


def test_credential_shaped_request_is_not_sent(monkeypatch):
    calls = _fake(monkeypatch, lambda qid, c: ("docx", 0.9))
    d = skill_router.route_skill("use key sk-live-abcdefghijklmnopqrstuvwxyz0123456789", SKILLS)  # pragma: allowlist secret
    assert d["selected"] == "none" and d["source"] == "policy" and calls == []


def test_large_catalog_runs_groups_then_a_final_between_winners(monkeypatch):
    monkeypatch.setattr(skill_router, "GROUP_SIZE", 30)
    skills = {f"s{i:03d}": f"skill number {i}" for i in range(70)}
    favourites = {"s005", "s040"}

    def answer(qid, criteria):
        if qid == "final":
            return ("s040", 0.7)
        hit = favourites & set(criteria)
        return (hit.pop(), 0.8) if hit else ("none", 0.9)

    calls = _fake(monkeypatch, answer)
    d = skill_router.route_skill("x", skills)
    assert d["groups"] == 3 and d["rounds"] == 2 and d["selected"] == "s040"
    group_questions = [q for call in calls for qid, q in call.items() if qid != "final"]
    assert len(group_questions) == 3 and all("none" in q for q in group_questions)
    [final] = [call["final"] for call in calls if "final" in call]
    assert set(final) == favourites | {"none"}


def test_a_lone_group_winner_skips_the_final(monkeypatch):
    monkeypatch.setattr(skill_router, "GROUP_SIZE", 30)
    skills = {f"s{i:03d}": f"skill number {i}" for i in range(70)}
    calls = _fake(monkeypatch, lambda qid, c: ("s005", 0.75) if "s005" in c else ("none", 0.9))
    d = skill_router.route_skill("x", skills)
    assert d["selected"] == "s005" and d["rounds"] == 1
    assert not any("final" in call for call in calls)


def test_seventy_skills_go_in_one_question(monkeypatch):
    """A Choice takes 255 options; splitting a list that fits only loses context."""
    calls = _fake(monkeypatch, lambda qid, c: ("s005", 0.9))
    d = skill_router.route_skill("x", {f"s{i:03d}": f"skill {i}" for i in range(70)})
    assert d["groups"] == 1 and len(calls) == 1 and len(calls[0]["g0"]) == 71


def test_bar_is_on_probability_not_confidence(monkeypatch):
    """Confidence shrinks with fewer options; the chosen skill's probability does not."""
    def decide(state, questions):
        return Decision({"g0": Answer("choice", "xlsx", 0.40, {"xlsx": 0.65, "docx": 0.30, "none": 0.05})},
                        "jev", "gateway", 1, 1)

    monkeypatch.setattr(skill_router, "decide", decide)
    d = skill_router.route_skill("chart this csv", SKILLS)
    assert d["selected"] == "xlsx" and d["confidence"] == 0.65 and d["jev_confidence"] == 0.4
    assert d["separation"] == round(0.65 / 0.30, 2)


def test_beam_lets_a_group_runner_up_win_the_final(monkeypatch):
    monkeypatch.setattr(skill_router, "GROUP_SIZE", 30)
    skills = {f"s{i:03d}": f"skill number {i}" for i in range(70)}

    def decide(state, questions):
        out = {}
        for qid, q in questions.items():
            if qid == "final":
                out[qid] = Answer("choice", "s002", 0.5, {"s002": 0.8, "s001": 0.15, "none": 0.05})
            elif "s001" in q.criteria:
                out[qid] = Answer("choice", "s001", 0.4, {"s001": 0.5, "s002": 0.45, "none": 0.05})
            else:
                out[qid] = Answer("choice", "none", 0.9, {"none": 0.95, "s040": 0.05})
        return Decision(out, "jev", "gateway", 1, 1)

    monkeypatch.setattr(skill_router, "decide", decide)
    d = skill_router.route_skill("x", skills)
    assert d["rounds"] == 2 and d["selected"] == "s002"
    assert d["confidence"] == round((0.45 * 0.8) ** 0.5, 3)


# ------------------------------------------------------------------ switch and hook

def test_switch_is_off_by_default_and_persists(monkeypatch):
    assert skill_router.enabled() is False
    skill_router.set_enabled(True)
    assert skill_router.enabled() is True
    monkeypatch.setenv("JEV_SKILL_ROUTER", "0")
    assert skill_router.enabled() is False


def _run_prompt_hook(monkeypatch, capsys):
    import hooks

    def no_repo_call(state, questions):
        raise TransportError("offline")

    monkeypatch.setattr(hooks, "_have_key", lambda: True)
    monkeypatch.setattr(core, "decide", no_repo_call)
    hooks.prompt({"prompt": "please turn these notes into a word document", "cwd": "/tmp"})
    return capsys.readouterr().out


def test_hook_is_silent_while_switched_off(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(skill_router, "route_skill", lambda *a, **k: called.append(1))
    assert _run_prompt_hook(monkeypatch, capsys) == "" and called == []


def test_hook_adds_the_one_line_note_when_on(monkeypatch, capsys):
    skill_router.set_enabled(True)
    monkeypatch.setattr(skill_router, "route_skill", lambda text, project=None: {
        "selected": "docx", "confidence": 0.8})
    out = json.loads(_run_prompt_hook(monkeypatch, capsys))["hookSpecificOutput"]
    assert out["hookEventName"] == "UserPromptSubmit"
    assert out["additionalContext"].startswith("jev: skill `docx` (80% sure)")


def test_hook_survives_a_broken_router(monkeypatch, capsys):
    skill_router.set_enabled(True)

    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(skill_router, "route_skill", broken)
    assert _run_prompt_hook(monkeypatch, capsys) == ""


# ------------------------------------------------------------------ servers and test harness

@pytest.mark.parametrize("server", ["mcp_server", "remote_server"])
def test_route_skill_is_exposed(monkeypatch, server):
    import importlib

    mod = importlib.import_module(server)
    _fake(monkeypatch, lambda qid, c: ("pdf", 0.9))
    d = mod.jev_route_skill("merge these two pdfs", skills=SKILLS)
    assert d.selected == "pdf" and d.note and d.source == "model"


def test_evaluate_labels_outcomes_and_sweeps_the_bar(monkeypatch):
    answers = {"make a word doc": ("docx", 0.9), "merge pdfs": ("docx", 0.7),
               "chart this csv": ("xlsx", 0.5), "hello there": ("none", 0.8)}
    monkeypatch.setattr(skill_router, "decide", lambda state, q: Decision(
        {"g0": _choice(*answers[state["request"]])}, "jev", "gateway", 1, 1))
    rows = skill_router.evaluate([
        {"request": "make a word doc", "expected": "docx"},
        {"request": "merge pdfs", "expected": "pdf"},
        {"request": "chart this csv", "expected": "xlsx"},
        {"request": "hello there", "expected": "none"},
    ], SKILLS)
    assert [r["outcome"] for r in rows] == ["hit", "wrong-above-bar", "fell back", "hit"]
    by_bar = {s["bar"]: s for s in skill_router.sweep(rows)}
    assert by_bar[0.5]["hit"] == 3 and by_bar[0.8]["wrong-above-bar"] == 0
    assert "wrong-above-bar" in skill_router.table(rows)
