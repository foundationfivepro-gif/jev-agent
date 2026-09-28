#!/usr/bin/env python3
"""
Simulate the skill router against a labelled set of requests, live on Jev.

    python3 scripts/simulate_skills.py                         # default cases, once each
    python3 scripts/simulate_skills.py --repeat 3 --workers 4  # stability + load
    python3 scripts/simulate_skills.py --cases my.json --json report.json

Cases are [{"request", "expected", "kind"?, "note"?}], where expected is a skill
name as Claude Code loads it (its folder name) or "none", and kind is a label
to group results by: clear, near-miss, none. Cases whose expected skill is not
installed here are skipped and listed, so a case file travels between machines.

Every call runs with a generous deadline (--measure-ms), so the report can say
what ANY time limit would have kept, not only the configured one. Then:

  outcomes   hit / wrong-above-bar / fell back at the configured bar and limit
  by kind    accuracy for clear, near-miss and none separately; near-miss is
             the number that matters, since clear cases are easy for anyone
  confusion  expected -> picked for every miss; fix these with sharper
             `description:` lines in the skills involved
  stability  requests whose pick changed between repeats
  bars       the same answers at other bars (0.4 .. 0.8)
  deadlines  share of calls each limit would have kept, plus latency percentiles

Jev only points: nothing here runs a skill or writes anything but the optional
JSON report and the router's traces. Each call is one Jev request (one per group
if the catalog is split), so the call count is printed before anything is sent.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import skill_router  # noqa: E402
from core import active_transport  # noqa: E402
from skill_router import NONE, outcome  # noqa: E402

DEFAULT_CASES = Path(__file__).resolve().parent / "skill_sim_cases.json"
DEADLINES_MS = (500, 800, 1000, 1500)


def load_cases(path: Path, catalog: dict) -> tuple[list[dict], list[dict]]:
    return load_cases_from(json.loads(path.read_text(encoding="utf-8")), catalog)


def load_cases_from(cases: list[dict], catalog: dict) -> tuple[list[dict], list[dict]]:
    usable, skipped = [], []
    for c in cases:
        c = {"kind": "unlabelled", **c, "expected": c.get("expected") or NONE}
        (usable if c["expected"] == NONE or c["expected"] in catalog else skipped).append(c)
    return usable, skipped


def run(cases: list[dict], catalog: dict, *, repeat: int, workers: int, measure_ms: int) -> list[dict]:
    jobs = [(i, c) for i, c in enumerate(cases) for _ in range(repeat)]

    def one(job):
        i, c = job
        [row] = skill_router.evaluate([c], catalog, deadline_ms=measure_ms)
        return {**row, "case": i, "kind": c["kind"], "note": c.get("note")}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, jobs))


def at_limits(rows: list[dict], bar: float, limit_ms: int) -> list[dict]:
    """Re-score measured rows as if the router had run with this bar and time limit."""
    out = []
    for r in rows:
        late = r["latency_ms"] is not None and r["latency_ms"] > limit_ms
        ok = r["source"] == "model" and not late and r["confidence"] >= bar and r["proposed"] not in (None, NONE)
        picked = r["proposed"] if ok else NONE
        out.append({**r, "picked": picked, "late": late, "outcome": outcome(r["expected"], picked)})
    return out


def percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def report(rows: list[dict], skipped: list[dict], *, bar: float, limit_ms: int, repeat: int) -> dict:
    scored = at_limits(rows, bar, limit_ms)
    total = Counter(r["outcome"] for r in scored)

    by_kind: dict[str, Counter] = defaultdict(Counter)
    for r in scored:
        by_kind[r["kind"]][r["outcome"]] += 1

    confusion = Counter((r["expected"], r["picked"], r["kind"]) for r in scored if r["outcome"] != "hit")

    picks_per_case: dict[int, set] = defaultdict(set)
    for r in rows:
        picks_per_case[r["case"]].add(r["proposed"])
    unstable = [(rows_for[0]["request"], sorted(map(str, picks)))
                for case, picks in picks_per_case.items() if len(picks) > 1
                for rows_for in [[r for r in rows if r["case"] == case]]]

    measured = [r["latency_ms"] for r in rows if r["latency_ms"] is not None and r["source"] == "model"]
    bars = []
    for b in (0.4, 0.5, 0.6, 0.7, 0.8):
        c = Counter(r["outcome"] for r in at_limits(rows, b, limit_ms))
        bars.append({"bar": b, "hit": c["hit"], "wrong-above-bar": c["wrong-above-bar"], "fell back": c["fell back"]})

    return {
        "calls": len(rows), "cases": len(rows) // max(repeat, 1), "repeat": repeat,
        "bar": bar, "limit_ms": limit_ms,
        "outcomes": dict(total),
        "by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
        "confusion": [{"expected": e, "picked": p, "kind": k, "count": n} for (e, p, k), n in confusion.most_common()],
        "unstable": [{"request": q, "picks": p} for q, p in unstable],
        "bars": bars,
        "deadlines": {str(d): round(sum(x <= d for x in measured) / len(measured), 3) if measured else None
                      for d in DEADLINES_MS},
        "latency_ms": {"p50": percentile(measured, 0.5), "p90": percentile(measured, 0.9),
                       "max": max(measured) if measured else None},
        "errors": Counter(r["source"] for r in rows if r["source"] not in ("model", "policy")),
        "skipped": [c["request"] for c in skipped],
        "rows": scored,
    }


def render(rep: dict, catalog: dict) -> str:
    o, n = rep["outcomes"], rep["calls"] or 1
    lines = [
        f"# Skill router simulation — {rep['cases']} cases x {rep['repeat']} = {rep['calls']} calls",
        f"bar {rep['bar']}, limit {rep['limit_ms']}ms",
        "",
        f"hit {o.get('hit', 0)}/{n} ({100 * o.get('hit', 0) // n}%) · "
        f"wrong-above-bar {o.get('wrong-above-bar', 0)} · fell back {o.get('fell back', 0)}",
        "",
        "## By kind",
        "| kind | hit | wrong-above-bar | fell back |", "|---|---|---|---|",
    ]
    for kind, c in rep["by_kind"].items():
        lines.append(f"| {kind} | {c.get('hit', 0)} | {c.get('wrong-above-bar', 0)} | {c.get('fell back', 0)} |")

    lines += ["", "## Misses (expected -> picked)"]
    if not rep["confusion"]:
        lines.append("none")
    for m in rep["confusion"]:
        lines.append(f"- {m['expected']} -> {m['picked']}  x{m['count']}  ({m['kind']})")
    for name in sorted({x for m in rep["confusion"] for x in (m["expected"], m["picked"])} - {NONE}):
        if name in catalog:
            lines.append(f"  - `{name}` description now: {catalog[name]['description'][:200]}")

    if rep["repeat"] > 1:
        lines += ["", "## Stability across repeats"]
        lines += [f"- {u['request'][:70]}: {u['picks']}" for u in rep["unstable"]] or ["every pick repeated"]

    lines += ["", "## Bars (same answers)", "| bar | hit | wrong-above-bar | fell back |", "|---|---|---|---|"]
    lines += [f"| {b['bar']} | {b['hit']} | {b['wrong-above-bar']} | {b['fell back']} |" for b in rep["bars"]]

    lat = rep["latency_ms"]
    lines += ["", "## Latency",
              f"p50 {lat['p50']}ms · p90 {lat['p90']}ms · max {lat['max']}ms",
              "kept by limit: " + " · ".join(f"{d}ms {round(100 * s)}%" for d, s in rep["deadlines"].items()
                                            if s is not None)]
    if rep["errors"]:
        lines.append(f"errors: {dict(rep['errors'])}")
    if rep["skipped"]:
        lines += ["", f"## Skipped ({len(rep['skipped'])}: expected skill not installed here)"]
        lines += [f"- {q}" for q in rep["skipped"]]

    lines += ["", "## Every call", "| # | kind | request | expected | picked | p | ms | outcome |",
              "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rep["rows"], 1):
        picked = r["picked"] if r["picked"] != NONE or r["proposed"] in (None, NONE) else f"none (Jev: {r['proposed']})"
        lines.append(f"| {i} | {r['kind']} | {r['request'][:60]} | {r['expected']} | {picked} | "
                     f"{round(100 * r['confidence'])}% | {r['latency_ms']} | {r['outcome']} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    ap.add_argument("--project", default=str(ROOT), help="project whose skills/ are included")
    ap.add_argument("--repeat", type=int, default=1, help="run each case N times (stability, latency spread)")
    ap.add_argument("--workers", type=int, default=1, help="concurrent calls (1 = realistic per-message latency)")
    ap.add_argument("--bar", type=float, default=skill_router.MIN_PROBABILITY)
    ap.add_argument("--limit-ms", type=int, default=skill_router.DEADLINE_MS, help="time limit to score against")
    ap.add_argument("--measure-ms", type=int, default=5000, help="deadline actually used while measuring")
    ap.add_argument("--json", type=Path, help="also write the full report here")
    args = ap.parse_args(argv)

    try:
        import hooks

        hooks._load_env()
    except Exception:
        pass
    if not active_transport():
        print("No Jev key: set TYPESAFE_API_KEY (or the legacy AI_GATEWAY_API_KEY).", file=sys.stderr)
        return 2

    catalog = skill_router.build_catalog(args.project)
    cases, skipped = load_cases(args.cases, catalog)
    print(f"{len(catalog)} skills installed; {len(cases)} cases x {args.repeat} = "
          f"{len(cases) * args.repeat} Jev calls ({len(skipped)} cases skipped)", file=sys.stderr)

    rows = run(cases, catalog, repeat=args.repeat, workers=args.workers, measure_ms=args.measure_ms)
    rep = report(rows, skipped, bar=args.bar, limit_ms=args.limit_ms, repeat=args.repeat)
    print(render(rep, catalog))
    if args.json:
        args.json.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
