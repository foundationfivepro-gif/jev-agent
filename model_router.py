"""
Cost-aware routing for the EXECUTOR model — the coding model that does the work.

Jev is the only model this package calls. It is not a candidate here; it is the
thing choosing among the executors you have configured.

The decision is not "is this task easy" but "what is the cheapest route that
passes, counting everything". Two costs are routinely left out:

  1. Context re-processing. Handing a subtask to a cheaper model and bringing
     the result back means the expensive model pays full input price to absorb
     it. For a long warm context that can exceed what was saved.
  2. Retries. A cheaper model that fails acceptance and escalates costs the
     cheap attempt plus the expensive one plus the operator's time.

Whether routing wins is sensitive to the price gap and flips inside the range of
real prices, so `estimate_costs` computes it from the catalog rather than
encoding a verdict. Run it with your own numbers before trusting either answer.
"""

from __future__ import annotations

import json
from typing import Mapping

from core import Choice, Score, decide, write_trace

MIN_CONFIDENCE = 0.75

# cost_in / cost_out are per million tokens.
DEFAULT_CATALOG: dict[str, dict] = {
    "fast":     {"fit": "Classification, formatting, simple mechanical edits",
                 "cost_in": 1.0, "cost_out": 5.0, "tier": 1, "browser": False},
    "balanced": {"fit": "Normal coding, research, multi-file edits",
                 "cost_in": 3.0, "cost_out": 15.0, "tier": 2, "browser": True},
    "strong":   {"fit": "Complex architecture, hard debugging, subtle refactors",
                 "cost_in": 15.0, "cost_out": 75.0, "tier": 3, "browser": True},
}


def estimate_costs(
    catalog: Mapping[str, Mapping], *, context_mtok: float, output_mtok: float, tool_mtok: float
) -> dict:
    """
    Compare doing the whole task on each model against delegating generation to
    the cheapest one and having the strongest absorb the result.

    Returns per-model totals plus the delegated total, so a caller can see
    whether routing actually pays at *their* prices and context size.
    """
    names = sorted(catalog, key=lambda k: catalog[k]["tier"])
    cheap, strong = catalog[names[0]], catalog[names[-1]]

    pure = {
        n: catalog[n]["cost_out"] * output_mtok + catalog[n]["cost_in"] * tool_mtok
        for n in names
    }
    delegated = (
        cheap["cost_in"] * context_mtok          # cheap model loads the context
        + cheap["cost_out"] * output_mtok        # cheap model generates
        + cheap["cost_in"] * tool_mtok           # cheap model reads tool output
        + strong["cost_in"] * (output_mtok + tool_mtok)   # strong model re-reads the result
    )
    best_pure = min(pure, key=pure.get)
    return {
        "pure": pure,
        "delegated": delegated,
        "delegation_wins": delegated < pure[names[-1]],
        "cheapest_pure": best_pure,
        "note": "Delegation pays only when the returned result is much smaller "
                "than the context it was derived from. Compression ratio decides "
                "this, not task difficulty.",
    }


def route_model(
    task: str,
    *,
    catalog: Mapping[str, Mapping] = DEFAULT_CATALOG,
    needs_browser: bool = False,
    max_cost_in: float = 1e9,
    min_confidence: float = MIN_CONFIDENCE,
) -> dict:
    """Choose the cheapest eligible executor that should succeed."""
    eligible = {
        k: v for k, v in catalog.items()
        if v["cost_in"] <= max_cost_in and (v["browser"] or not needs_browser)
    }
    if not eligible:
        decision = {"selected": "human", "reason": "no eligible model", "source": "policy"}
        write_trace("model_router", {"task": task}, decision)
        return decision

    criteria = {k: v["fit"] for k, v in eligible.items()}
    criteria["human"] = "None of these should attempt this unaided"

    state = {
        "task": task,
        "needs_browser": needs_browser,
        "candidates": {k: {"fit": v["fit"], "relative_cost": v["cost_out"]} for k, v in eligible.items()},
        "objective": "lowest total cost that still passes acceptance",
    }
    result = decide(state, {
        "model": Choice(instructions="Which eligible model is the cheapest sufficient route?",
                        criteria=criteria),
        "complexity": Score(instructions="Complexity of the complete task",
                            criteria=["mechanical", "standard", "multi-step", "frontier"]),
    })

    answer = result.answers["model"]
    # Fail closed toward capability: an uncertain route goes to the strongest
    # eligible model, because a cheap failure costs the cheap attempt plus the
    # expensive retry plus the latency of noticing.
    strongest = max(eligible, key=lambda k: eligible[k]["tier"])
    selected = str(answer.value) if answer.certainty >= min_confidence else strongest
    if selected not in eligible and selected != "human":
        selected = strongest

    decision = {
        "selected": selected,
        "proposed": answer.value,
        "confidence": answer.certainty,
        "complexity": result.value("complexity"),
        "probabilities": answer.probabilities,
        "source": "model",
    }
    write_trace("model_router", state, decision)
    return decision


if __name__ == "__main__":
    print(json.dumps(route_model("Refactor the auth service and add tests"), indent=2, default=str))
    print(json.dumps(
        estimate_costs(DEFAULT_CATALOG, context_mtok=0.65, output_mtok=0.12, tool_mtok=0.23),
        indent=2, default=str))
