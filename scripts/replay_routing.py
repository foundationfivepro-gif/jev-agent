#!/usr/bin/env python3
"""
Replay recorded routing decisions under other thresholds. Offline: no Jev call,
no Claude call, nothing written.

    python3 scripts/replay_routing.py                    # ~/.jev/traces, default grid
    python3 scripts/replay_routing.py --dir path/to/traces --json replay.json
    python3 scripts/replay_routing.py --min-confidence 0.7 --top-tier-mass 0.5

Every routing trace keeps Jev's answer (proposed model, certainty, probabilities,
complexity), so `model_router.select` can rerun it under any threshold and say
what each variant would have picked. The hook's outcome traces add how the
subagent ended and how many tokens it used.

What this can tell you:
  fidelity     replaying the current thresholds reproduces the recorded picks;
               mismatches are decisions logged under older thresholds or another
               JEV_MODELS menu, and are listed so they are not mistaken for a result
  mix + cost   per variant: which models would have run, and an estimated bill
  moves        routes a variant sends to a cheaper or a stronger tier than ran
  calibration  how often accepted routes came back ok, by tier and Jev certainty

What it cannot: whether a cheaper model would have done the work. A moved route
never ran on its new model, so its cost is an estimate (recorded tokens at the
new model's input rate, the same pricing `hooks.py report` uses) and its quality
is unknown. "ok" means the subagent returned non-empty output without an error,
not that the output was right. Treat a cheaper variant as a hypothesis to test
on real tasks, not a result.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import model_router  # noqa: E402
from model_router import available_catalog, select  # noqa: E402

GRID = {
    "min_confidence": (0.6, 0.65, 0.7, 0.75, 0.8),
    "mechanical_confidence": (0.4, 0.5, 0.6),
    "min_top_tier_mass": (0.3, 0.4, 0.5),
}
CERTAINTY_BUCKETS = (0.0, 0.5, 0.6, 0.7, 0.75, 0.8, 0.9, 1.01)


def current_policy() -> dict:
    return {
        "min_confidence": model_router.MIN_CONFIDENCE,
        "mechanical_confidence": model_router.MECHANICAL_CONFIDENCE,
        "min_top_tier_mass": model_router.MIN_TOP_TIER_FALLBACK_MASS,
    }


def load(trace_dir: Path) -> list[dict]:
    """Routing decisions Jev made, each joined to its outcome when one was recorded."""
    routes, outcomes = {}, {}
    for path in sorted(trace_dir.glob("model_router*.json")) if trace_dir.is_dir() else []:
        try:
            t = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        key = (t.get("meta") or {}).get("tool_use_id") or path.name
        if t.get("system") == "model_router_outcome":
            outcomes[key] = t.get("result") or {}
        elif (t.get("decision") or {}).get("source") == "model":
            routes[key] = {"time": t.get("time"), **t["decision"]}
    return [{**d, "outcome": outcomes.get(k)} for k, d in routes.items()]


def replay(decisions: list[dict], catalog: dict, policy: dict) -> list[str]:
    return [
        select(str(d.get("proposed")), float(d.get("confidence") or 0.0),
               d.get("probabilities"), d.get("complexity"), catalog, **policy)[0]
        for d in decisions
    ]


def _rate(catalog: dict, model: str) -> float | None:
    return (catalog.get(model) or {}).get("cost_in")


def evaluate(decisions: list[dict], catalog: dict, policy: dict, recorded: list[str]) -> dict:
    picks = replay(decisions, catalog, policy)
    tier = {k: v["tier"] for k, v in catalog.items()}
    cost, priced, cheaper, stronger = 0.0, 0, 0, 0
    for d, ran, pick in zip(decisions, recorded, picks):
        tokens = int((d.get("outcome") or {}).get("tokens") or 0)
        rate = _rate(catalog, pick)
        if tokens and rate is not None:
            cost += tokens * rate / 1e6
            priced += 1
        if pick in tier and ran in tier and pick != ran:
            cheaper += tier[pick] < tier[ran]
            stronger += tier[pick] > tier[ran]
    return {
        "policy": policy,
        "mix": dict(Counter(picks)),
        "est_usd": round(cost, 4),
        "priced_routes": priced,
        "moved_cheaper": cheaper,
        "moved_stronger": stronger,
    }


def calibration(decisions: list[dict], recorded: list[str]) -> dict:
    """ok-rate of accepted routes (ran on Jev's own proposal), by tier and certainty."""
    cells: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for d, ran in zip(decisions, recorded):
        outcome = d.get("outcome")
        if not outcome or ran != d.get("proposed"):
            continue
        c = float(d.get("confidence") or 0.0)
        lo = max(b for b in CERTAINTY_BUCKETS if b <= c)
        hi = CERTAINTY_BUCKETS[CERTAINTY_BUCKETS.index(lo) + 1]
        band = f"{lo:.2f}-{min(hi, 1.0):.2f}"
        cells[ran][band][outcome.get("status", "unknown")] += 1
    return {m: {b: dict(c) for b, c in sorted(bands.items())} for m, bands in sorted(cells.items())}


def run(trace_dir: Path, grid: dict) -> dict:
    catalog = available_catalog()
    decisions = load(trace_dir)
    recorded = [str(d.get("selected")) for d in decisions]
    base = current_policy()

    replayed = replay(decisions, catalog, base)
    mismatches = [
        {"time": d.get("time"), "recorded": r, "replayed": p, "proposed": d.get("proposed"),
         "confidence": d.get("confidence")}
        for d, r, p in zip(decisions, recorded, replayed) if r != p
    ]
    # Cost the routes as they actually ran, at the same rate card, for the baseline row.
    actual = 0.0
    for d, ran in zip(decisions, recorded):
        tokens = int((d.get("outcome") or {}).get("tokens") or 0)
        rate = _rate(catalog, ran)
        if tokens and rate is not None:
            actual += tokens * rate / 1e6

    keys = list(grid)
    variants = [evaluate(decisions, catalog, dict(zip(keys, values)), recorded)
                for values in itertools.product(*(grid[k] for k in keys))]
    variants.sort(key=lambda v: (v["est_usd"], v["moved_cheaper"]))
    return {
        "trace_dir": str(trace_dir),
        "decisions": len(decisions),
        "with_outcome": sum(1 for d in decisions if d.get("outcome")),
        "current_policy": base,
        "actual": {"mix": dict(Counter(recorded)), "est_usd": round(actual, 4)},
        "fidelity": {"mismatches": len(mismatches), "examples": mismatches[:10]},
        "current": evaluate(decisions, catalog, base, recorded),
        "variants": variants,
        "calibration": calibration(decisions, recorded),
    }


def _row(name: str, v: dict, actual_usd: float) -> str:
    p = v["policy"]
    delta = f"{(v['est_usd'] / actual_usd - 1) * 100:+.0f}%" if actual_usd else "n/a"
    mix = " ".join(f"{m}:{n}" for m, n in sorted(v["mix"].items()))
    return (f"{name:<8} {p['min_confidence']:>5} {p['mechanical_confidence']:>5} "
            f"{p['min_top_tier_mass']:>5}  ${v['est_usd']:>8.4f} {delta:>6}  "
            f"{v['moved_cheaper']:>5} {v['moved_stronger']:>5}  {mix}")


def show(r: dict, top: int) -> None:
    print(f"{r['decisions']} routing decisions in {r['trace_dir']}, "
          f"{r['with_outcome']} with an outcome")
    f = r["fidelity"]
    print(f"fidelity: replaying current thresholds reproduces {r['decisions'] - f['mismatches']}"
          f"/{r['decisions']} recorded picks")
    if f["mismatches"]:
        print("  (mismatches were logged under older thresholds or another JEV_MODELS menu; "
              "see --json for examples)")
    if not r["with_outcome"]:
        print("\nNo outcome traces: costs below are $0. Outcomes are recorded by the "
              "agent-outcome hook (python3 hooks.py install).")

    actual = r["actual"]["est_usd"]
    print(f"\nactual spend on routed subagents (tokens x input rate): ${actual:.4f}\n")
    print(f"{'':<8} {'conf':>5} {'mech':>5} {'top':>5}  {'est $':>9} {'vs now':>6}  "
          f"{'down':>5} {'up':>5}  mix")
    print(_row("current", r["current"], actual))
    for i, v in enumerate(r["variants"][:top]):
        print(_row(f"#{i + 1}", v, actual))
    print("\ndown/up = routes moved to a cheaper/stronger tier than actually ran. Each 'down'")
    print("route never ran on its new model: its cost is an estimate, its quality unknown.")

    print("\ncalibration (accepted routes only; ok = non-empty, no error):")
    for model, bands in r["calibration"].items():
        cells = []
        for band, c in bands.items():
            n = sum(c.values())
            cells.append(f"{band} {c.get('ok', 0)}/{n}")
        print(f"  {model:<7} " + "   ".join(cells))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=os.environ.get("JEV_TRACE_DIR") or str(Path.home() / ".jev" / "traces"))
    ap.add_argument("--json", help="Also write the full result here.")
    ap.add_argument("--top", type=int, default=10, help="Variants to print, cheapest first.")
    ap.add_argument("--min-confidence", type=float, action="append")
    ap.add_argument("--mechanical-confidence", type=float, action="append")
    ap.add_argument("--top-tier-mass", type=float, action="append")
    args = ap.parse_args(argv)

    grid = dict(GRID)
    for key, values in (("min_confidence", args.min_confidence),
                        ("mechanical_confidence", args.mechanical_confidence),
                        ("min_top_tier_mass", args.top_tier_mass)):
        if values:
            grid[key] = tuple(values)

    result = run(Path(args.dir).expanduser(), grid)
    show(result, args.top)
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
