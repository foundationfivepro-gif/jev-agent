#!/usr/bin/env python3
"""
Skill router: Jev points at the one skill that should handle a request.

The same shape as tool_router.py. The catalog is built in code from the skills
actually installed, "none" is always an option, a name that is not in the
catalog becomes "none", and a low probability collapses to "none" rather than
to a guess. Jev only points: nothing here runs a skill or writes anything but a trace.

    python3 skill_router.py try "turn this csv into a chart"
    python3 skill_router.py list                     # what Jev would choose from
    python3 skill_router.py on | off | status        # the UserPromptSubmit suggestion
    python3 skill_router.py test cases.json          # [{"request", "expected"}] -> table

Catalog. Every SKILL.md under ~/.claude/skills (nested folders included, which
is where account-synced skills land), the project's .claude/skills and skills/,
and the skills of enabled plugins, named `plugin:skill` as the Skill tool names
them. It is re-read from disk on every call: frontmatter only, a few KB per
skill, so it can never be stale and there is no cache to invalidate.

The bar is on probability, not confidence. TypeSafe's Choice confidence is
roughly (n * peak - 1) / (n - 1), so it depends on the number of options: a 60%
peak is 0.58 confidence among 23 options and 0.40 among 3. A bar on confidence
would mean something different in every round. The chosen skill's probability
means the same thing at any size (docs.typesafe.ai/confidence).

Large catalogs. A Choice takes up to 255 options and the docs say to give it the
full list, so one question is the normal case. Only when the descriptions exceed
the gateway's request budget (MAX_PAYLOAD_CHARS, measured in core.py) are skills
split into groups, each with "none", asked in parallel. Following the TypeSafe
hierarchical-classification cookbook, this is a beam search, not a knockout: the
top BEAM skills of each group with at least FLOOR probability go to a final
round (again with "none"), and a finalist's score is the geometric mean of its
group and final probabilities, so a close call in one group can still win.

Latency. The whole thing, both rounds, runs under DEADLINE_MS (800ms). Past it
the answer is "none" with source "timeout" and the caller picks the normal way;
the unfinished call is abandoned, never awaited. The same holds for any
transport error: this router can make a pick worse, never block a message.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import MAX_PAYLOAD_CHARS, UNTRUSTED, Choice, TransportError, decide, write_trace  # noqa: E402

MIN_PROBABILITY = float(os.getenv("JEV_SKILL_MIN_PROBABILITY", "0.60"))
# Measured on api.typesafe.ai with 23 skills: 20 calls, 309-564ms, median 346.
# 500ms dropped 1 in 20 suggestions; 800ms kept all of them.
DEADLINE_MS = int(os.getenv("JEV_SKILL_DEADLINE_MS", "800"))
GROUP_SIZE = 254                # a Choice takes 255 options; one is "none"
BEAM = 2                        # candidates per group carried to the final round
FLOOR = 0.10                    # below this a group candidate is not worth a final slot
DESCRIPTION_CHARS = 400         # enough for "what it's for" and "when to use it"
MAX_REQUEST_CHARS = 2000
NONE = "none"
NONE_TEXT = "None of these skills is clearly needed for this request"

STATE_FILE = Path(os.getenv("JEV_SKILL_ROUTER_STATE") or Path.home() / ".jev" / "skill-router.json")

INSTRUCTIONS = (
    "Which one skill should handle the user request in `request`? Choose the skill whose "
    "description fits what the request asks for. Choose 'none' when no listed skill is clearly "
    "needed." + UNTRUSTED
)


# ------------------------------------------------------------------ catalog


def _frontmatter(path: Path) -> dict[str, str]:
    """name and description from a SKILL.md's YAML frontmatter, or {} if it has none."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            head = fh.read(16_000)
    except OSError:
        return {}
    m = re.match(r"\A---\s*\n(.*?)\n---\s*(\n|\Z)", head, re.S)
    if not m:
        return {}
    try:
        import yaml

        meta = yaml.safe_load(m.group(1))
    except Exception:
        meta = None
    if not isinstance(meta, dict):
        # Descriptions often contain an unquoted ': ', which is invalid YAML but
        # which Claude Code itself tolerates. Read the two keys line by line.
        meta = {}
        for line in m.group(1).splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip() in ("name", "description"):
                meta[key.strip()] = value.strip().strip("'\"")
    return {k: " ".join(str(meta[k]).split()) for k in ("name", "description") if meta.get(k)}


def _scan(root: Path, prefix: str = "") -> dict[str, dict]:
    found: dict[str, dict] = {}
    if not root.is_dir():
        return found
    for md in sorted(root.glob("**/SKILL.md")):
        if len(md.relative_to(root).parts) > 5:
            continue
        meta = _frontmatter(md)
        # The folder is the name Claude Code loads the skill by; a frontmatter
        # `name` can differ (session-start-hook/ declares startup-hook-skill),
        # and pointing at the frontmatter name names a skill that cannot load.
        name = md.parent.name
        if not meta.get("description") or name == NONE:
            continue
        found.setdefault(prefix + name, {"description": meta["description"], "path": str(md)})
    return found


def _enabled_plugins(home: Path, project: Path | None) -> dict[str, bool]:
    enabled: dict[str, bool] = {}
    for settings in (home / ".claude" / "settings.json",
                     *((project / ".claude" / "settings.json",
                        project / ".claude" / "settings.local.json") if project else ())):
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for key, on in (data.get("enabledPlugins") or {}).items():
            enabled[key] = bool(on)
    return enabled


def _plugin_skills(home: Path, project: Path | None) -> dict[str, dict]:
    """Skills of installed plugins that are enabled, as `plugin:skill`."""
    registry = home / ".claude" / "plugins" / "installed_plugins.json"
    try:
        installed = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    installed = installed.get("plugins", installed) if isinstance(installed, dict) else {}
    enabled = _enabled_plugins(home, project)
    found: dict[str, dict] = {}
    for key, entries in installed.items():
        if not enabled.get(key, False):
            continue
        for entry in entries if isinstance(entries, list) else [entries]:
            path = entry.get("installPath") if isinstance(entry, dict) else None
            if path:
                found.update(_scan(Path(path) / "skills", prefix=key.split("@")[0] + ":"))
    return found


def build_catalog(project: str | Path | None = None, home: str | Path | None = None) -> dict[str, dict]:
    """{skill name: {description, path}} from disk, project skills first."""
    home_p = Path(home or Path.home()).expanduser()
    proj = Path(project).expanduser().resolve() if project else None
    catalog: dict[str, dict] = {}
    if proj:
        for root in (proj / ".claude" / "skills", proj / "skills"):
            for name, meta in _scan(root).items():
                catalog.setdefault(name, meta)
    for name, meta in _scan(home_p / ".claude" / "skills").items():
        catalog.setdefault(name, meta)
    for name, meta in _plugin_skills(home_p, proj).items():
        catalog.setdefault(name, meta)
    return catalog


# ------------------------------------------------------------------ routing


def _groups(names: Sequence[str], descriptions: Mapping[str, str]) -> list[list[str]]:
    budget = MAX_PAYLOAD_CHARS * 3 // 4  # a group must fit in one call with room to spare
    groups: list[list[str]] = []
    group: list[str] = []
    size = 0
    for name in names:
        cost = len(name) + len(descriptions[name]) + 16
        if group and (len(group) >= GROUP_SIZE or size + cost > budget):
            groups.append(group)
            group, size = [], 0
        group.append(name)
        size += cost
    if group:
        groups.append(group)
    return groups


def _question(names: Sequence[str], descriptions: Mapping[str, str]) -> Choice:
    criteria = {n: descriptions[n] for n in names}
    criteria[NONE] = NONE_TEXT
    return Choice(instructions=INSTRUCTIONS, criteria=criteria)


def _calls(groups: list[list[str]], descriptions: Mapping[str, str]) -> list[dict[str, Choice]]:
    """Pack group questions into as few requests as the payload budget allows."""
    calls: list[dict[str, Choice]] = []
    current: dict[str, Choice] = {}
    size = 0
    for i, group in enumerate(groups):
        cost = sum(len(n) + len(descriptions[n]) + 16 for n in group)
        if current and size + cost > MAX_PAYLOAD_CHARS * 3 // 4:
            calls.append(current)
            current, size = {}, 0
        current[f"g{i}"] = _question(group, descriptions)
        size += cost
    if current:
        calls.append(current)
    return calls


def _parallel(state: Any, calls: list[dict[str, Choice]], deadline: float) -> dict[str, Any] | None:
    """Run the calls at once; None if any errors or the deadline passes first."""
    results: list[Any] = [None] * len(calls)

    def run(i: int) -> None:
        try:
            results[i] = decide(state, calls[i])
        except Exception as exc:  # reported as a failure, never raised into the caller
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    if any(t.is_alive() for t in threads):
        return None
    answers: dict[str, Any] = {}
    for r in results:
        if isinstance(r, Exception):
            raise r
        answers.update(r.answers)
    return answers


def _none(reason: str, source: str, **extra: Any) -> dict:
    return {"selected": NONE, "proposed": None, "confidence": 0.0, "reason": reason,
            "source": source, **extra}


def _distribution(answer: Any) -> dict[str, float]:
    probs = {str(k): float(v) for k, v in (answer.probabilities or {}).items()}
    return probs or {str(answer.value): answer.certainty}


def _separation(probs: Mapping[str, float]) -> float | None:
    """Top probability over the runner-up: near 1x is ambiguous (the cookbook's metric)."""
    top = sorted(probs.values(), reverse=True)
    if len(top) < 2:
        return None
    return round(top[0] / top[1], 2) if top[1] > 0 else None


def route_skill(
    request: str,
    catalog: Mapping[str, Mapping[str, Any] | str] | None = None,
    *,
    project: str | Path | None = None,
    min_probability: float = MIN_PROBABILITY,
    deadline_ms: int = DEADLINE_MS,
) -> dict:
    """
    Pick at most one skill for `request`.

    `catalog` maps skill name -> description (or {description, ...}); omitted,
    it is read from disk. Never raises: every failure is "none" with a source
    that says why, so the caller picks the normal way. `confidence` in the
    result is the probability the bar was applied to; `jev_confidence` is Jev's
    own concentration statistic for the deciding question.
    """
    started = time.monotonic()
    deadline = started + deadline_ms / 1000

    def done(decision: dict) -> dict:
        decision["latency_ms"] = int((time.monotonic() - started) * 1000)
        decision["min_probability"] = min_probability
        write_trace("skill_router", {"request": request}, decision)
        return decision

    text = (request or "").strip()[:MAX_REQUEST_CHARS]
    if not text:
        return done(_none("empty request", "policy"))
    from security_router import SECRET, classify

    if classify([], text)[0] == SECRET:
        return done(_none("request looks credential-shaped; not sent", "policy"))

    raw = build_catalog(project) if catalog is None else catalog
    descriptions = {
        str(name): " ".join(str(meta.get("description", "") if isinstance(meta, Mapping) else meta).split())
        [:DESCRIPTION_CHARS]
        for name, meta in raw.items() if str(name) != NONE
    }
    descriptions = {n: d or n for n, d in descriptions.items()}
    if not descriptions:
        return done(_none("no skills installed", "policy"))

    names = sorted(descriptions)
    groups = _groups(names, descriptions)
    state = {"request": text}
    try:
        first = _parallel(state, _calls(groups, descriptions), deadline)
        if first is None:
            return done(_none(f"no answer within {deadline_ms}ms", "timeout", rounds=1))

        if len(groups) == 1:
            a = first["g0"]
            probs = _distribution(a)
            proposed = str(a.value)
            score, jev_conf, rounds = probs.get(proposed, 0.0), a.certainty, 1
        else:
            finalists: dict[str, float] = {}
            for i, group in enumerate(groups):
                a = first.get(f"g{i}")
                if a is None:
                    continue
                ranked = sorted(((p, n) for n, p in _distribution(a).items() if n in group), reverse=True)
                finalists.update({n: p for p, n in ranked[:BEAM] if p >= FLOOR})
            if not finalists:
                return done(_none("none of these", "model", rounds=1, groups=len(groups)))
            if len(finalists) == 1:
                [(proposed, score)] = finalists.items()
                probs, jev_conf, rounds = dict(finalists), None, 1
            else:
                final = _parallel(state, [{"final": _question(sorted(finalists), descriptions)}], deadline)
                if final is None:
                    return done(_none(f"final round missed the {deadline_ms}ms deadline", "timeout",
                                      rounds=2, finalists=sorted(finalists)))
                a = final["final"]
                probs = _distribution(a)
                proposed, jev_conf, rounds = str(a.value), a.certainty, 2
                # Geometric mean of the path's two decisions, as in the cookbook's beam search.
                score = (finalists[proposed] * probs.get(proposed, 0.0)) ** 0.5 if proposed in finalists else 0.0
    except TransportError as exc:
        return done(_none(f"Jev unavailable ({exc})", "unavailable"))
    except Exception as exc:  # a malformed answer is no answer
        return done(_none(f"{type(exc).__name__}: {exc}", "unavailable"))

    # A name back from the model is checked against the catalog before anyone acts on it.
    valid = proposed in descriptions
    selected = proposed if valid and score >= min_probability else NONE
    reason = ("picked" if selected != NONE
              else "not in the skill list" if proposed != NONE and not valid
              else "below the bar" if valid else "none of these")
    top = dict(sorted(probs.items(), key=lambda kv: -kv[1])[:5])
    return done({
        "selected": selected, "proposed": proposed, "confidence": round(score, 3),
        "jev_confidence": None if jev_conf is None else round(jev_conf, 3),
        "separation": _separation(probs), "reason": reason, "source": "model",
        "rounds": rounds, "groups": len(groups), "skills": len(descriptions),
        "probabilities": {k: round(v, 3) for k, v in top.items()},
        "path": raw.get(selected, {}).get("path") if isinstance(raw.get(selected), Mapping) else None,
    })


def note(decision: Mapping[str, Any]) -> str | None:
    """The one line shown when a skill is picked; None when the normal pick applies."""
    if decision.get("selected", NONE) == NONE:
        return None
    return (f"jev: skill `{decision['selected']}` ({round(100 * decision['confidence'])}% sure) "
            "fits this request; load it unless it clearly does not.")


# ------------------------------------------------------------------ switch


def enabled() -> bool:
    """The per-message suggestion. Off unless switched on."""
    env = os.getenv("JEV_SKILL_ROUTER")
    if env is not None:
        return env.strip().lower() in ("1", "on", "true", "yes")
    try:
        return bool(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("enabled", False))
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def set_enabled(on: bool) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"enabled": on}) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ test


def outcome(expected: str, selected: str) -> str:
    if selected == expected:
        return "hit"
    return "fell back" if selected == NONE else "wrong-above-bar"


def evaluate(cases: Sequence[Mapping[str, str]], catalog: Mapping[str, Any], **kw: Any) -> list[dict]:
    rows = []
    for case in cases:
        d = route_skill(case["request"], catalog, **kw)
        rows.append({"request": case["request"], "expected": case.get("expected") or NONE,
                     "picked": d["selected"], "proposed": d.get("proposed"),
                     "confidence": d["confidence"], "source": d["source"],
                     "separation": d.get("separation"), "latency_ms": d.get("latency_ms"),
                     "outcome": outcome(case.get("expected") or NONE, d["selected"])})
    return rows


def sweep(rows: Sequence[Mapping[str, Any]], bars: Sequence[float] = (0.4, 0.5, 0.6, 0.7, 0.8)) -> list[dict]:
    """
    What each bar would have done with the same answers. Approximate for
    catalogs split into groups, where the bar also filters group winners.
    """
    out = []
    for bar in bars:
        counts = {"hit": 0, "wrong-above-bar": 0, "fell back": 0}
        for r in rows:
            picked = r["proposed"] if r["source"] == "model" and r["confidence"] >= bar else NONE
            counts[outcome(r["expected"], picked or NONE)] += 1
        out.append({"bar": bar, **counts})
    return out


def table(rows: Sequence[Mapping[str, Any]]) -> str:
    lines = ["| # | request | expected | picked | confidence | outcome |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        picked = r["picked"] if r["picked"] != NONE or not r["proposed"] else f"none (Jev: {r['proposed']})"
        if r["source"] != "model":
            picked += f" [{r['source']}]"
        lines.append(f"| {i} | {r['request'][:70]} | {r['expected']} | {picked} | "
                     f"{round(100 * r['confidence'])}% | {r['outcome']} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("try")
    t.add_argument("request", nargs="+")
    sub.add_parser("list")
    sub.add_parser("on")
    sub.add_parser("off")
    sub.add_parser("status")
    e = sub.add_parser("test")
    e.add_argument("cases", help='JSON file: [{"request": ..., "expected": skill or "none"}]')
    for p in (t, sub.choices["list"], e):
        p.add_argument("--project", default=os.getenv("CLAUDE_PROJECT_DIR") or os.getcwd())
        p.add_argument("--deadline-ms", type=int, default=DEADLINE_MS)
    args = ap.parse_args(argv)

    if args.cmd in ("on", "off"):
        set_enabled(args.cmd == "on")
    if args.cmd in ("on", "off", "status"):
        print(f"skill suggestions on every message: {'on' if enabled() else 'off'}")
        return 0

    try:
        import hooks

        hooks._load_env()
    except Exception:
        pass
    catalog = build_catalog(args.project)
    if args.cmd == "list":
        for name, meta in sorted(catalog.items()):
            print(f"{name}: {meta['description'][:120]}")
        print(f"\n{len(catalog)} skills")
        return 0
    if args.cmd == "try":
        d = route_skill(" ".join(args.request), catalog, deadline_ms=args.deadline_ms)
        print(note(d) or f"jev: no skill ({d['reason']}; proposed {d.get('proposed')}, "
                         f"{round(100 * d['confidence'])}%, {d['source']}) — pick the normal way.")
        print(json.dumps(d, indent=2, default=str))
        return 0
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    rows = evaluate(cases, catalog, deadline_ms=args.deadline_ms)
    print(table(rows))
    print("\nbar sweep (same answers):")
    for s in sweep(rows):
        print(f"  {s['bar']:.2f}: {s['hit']} hit, {s['wrong-above-bar']} wrong-above-bar, {s['fell back']} fell back")
    for r in rows:
        if r["outcome"] != "hit":
            for name in {r["expected"], r["proposed"]} - {NONE, None}:
                if name in catalog:
                    print(f"\nmiss #{rows.index(r) + 1} — {name} description now:\n  {catalog[name]['description']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
