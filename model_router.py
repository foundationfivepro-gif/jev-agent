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
import os
from typing import Mapping

from core import UNTRUSTED, Choice, Score, decide, write_trace

MIN_CONFIDENCE = 0.75

# Below this complexity a task is mechanical, and a cheap-tier proposal is
# accepted at the lower bar: a Haiku retry on a one-line edit costs almost
# nothing, so failing toward capability there only spends Opus for no reason.
MECHANICAL_COMPLEXITY = 0.5
MECHANICAL_CONFIDENCE = 0.5

# Sonnet-first (owner decision 2026-10-02: Sonnet 5.5 handles everyday coding,
# multi-file edits, research and agentic tool use). An uncertain route lands on
# the floor tier — Sonnet — and climbs only where trying the floor first is
# expected to cost more than going straight up. Heuristic, pending calibration
# against outcomes: treating Jev's weight p on the higher tier as the chance the
# floor falls short, floor-first costs floor + p * higher and going up costs
# higher, so climbing pays from p >= 1 - floor/higher (0.5 at today's prices).
# It ignores token-volume differences and latency; it tracks the price list.
# Haiku is reached on an uncertain route only through the mechanical path.
FLOOR_TIER = "sonnet"

# cost_in / cost_out are USD per million tokens, Anthropic first-party rates.
# Keys are the names Claude Code's Agent tool accepts for its `model` parameter.
# Fits follow Anthropic's model selection matrix (docs: choosing-a-model).
# Fable is the top of the range, not a cheap tier: it costs 2.5x Opus. It is
# escalation_only — never offered to Jev and never selected (owner decision
# 2026-10-02: Fable was for one dev effort). A spawn that names it explicitly
# keeps it; the hook never overrides an explicit model.
DEFAULT_CATALOG: dict[str, dict] = {
    "haiku":  {"id": "claude-haiku-4-5",
               "fit": "Classification, formatting, simple mechanical edits, "
                      "high-volume or latency-sensitive sub-agent tasks that "
                      "return a short answer",
               "cost_in": 1.0, "cost_out": 5.0, "tier": 1},
    "sonnet": {"id": "claude-sonnet-5-5",
               "fit": "Everyday coding, research and agent work: code generation, "
                      "multi-file edits, data analysis, content creation, agentic tool use",
               "cost_in": 2.0, "cost_out": 10.0, "tier": 2},
    "opus":   {"id": "claude-opus-5-5",
               "fit": "Complex agentic coding: large-scale refactoring, complex systems "
                      "engineering and architecture, hard debugging, vision-heavy work, "
                      "computer use, work where a wrong answer is expensive to detect",
               "cost_in": 4.0, "cost_out": 20.0, "tier": 3},
    "fable":  {"id": "claude-fable-5-1",
               "fit": "Frontier work Opus falls short on: agent sessions that run for "
                      "hours, multistep deep research, analysis carried through to a "
                      "finished document, spreadsheet or deck, complex multi-page web "
                      "design where a wrong structural decision is expensive to unwind. "
                      "Never routine tasks, and not for ordinary multi-step work that "
                      "Opus handles",
               "cost_in": 10.0, "cost_out": 50.0, "tier": 4, "escalation_only": True},
}

def available_catalog(catalog: Mapping[str, Mapping] = DEFAULT_CATALOG) -> dict[str, dict]:
    """
    The menu as it stands now, not as it was written: only the models listed in
    JEV_MODELS (comma-separated, e.g. "haiku,sonnet"), when it is set. A router
    that can pick a model the account cannot run is choosing from yesterday's menu.
    """
    wanted = {m.strip().lower() for m in os.environ.get("JEV_MODELS", "").split(",") if m.strip()}
    # A restriction is never widened: a typo yields no route (the hook then uses
    # its own fallback), not models the account may not have.
    return {k: dict(v) for k, v in catalog.items() if not wanted or k in wanted}


def estimate_costs(
    catalog: Mapping[str, Mapping], *, context_mtok: float, output_mtok: float, tool_mtok: float,
    returned_mtok: float | None = None,
) -> dict:
    """
    Compare doing the whole task on each model against delegating generation to
    the cheapest one and having the strongest absorb the result.

    Returns per-model totals plus the delegated total, so a caller can see
    whether routing actually pays at *their* prices and context size.

    `returned_mtok` is what the parent actually reads back. Left unset, the parent
    absorbs all the delegate's output and tool reads — no compression at all.
    With a return contract it is the conclusion only, usually a few hundred tokens.
    """
    names = sorted(catalog, key=lambda k: catalog[k]["tier"])
    # The comparison is against the model the router actually falls back to,
    # so escalation-only tiers are not the benchmark.
    ordinary = [n for n in names if not catalog[n].get("escalation_only")] or names
    cheap, strong = catalog[names[0]], catalog[ordinary[-1]]

    pure = {
        n: catalog[n]["cost_out"] * output_mtok + catalog[n]["cost_in"] * tool_mtok
        for n in names
    }
    delegated = (
        cheap["cost_in"] * context_mtok          # cheap model loads the context
        + cheap["cost_out"] * output_mtok        # cheap model generates
        + cheap["cost_in"] * tool_mtok           # cheap model reads tool output
        + strong["cost_in"] * (output_mtok + tool_mtok if returned_mtok is None
                               else returned_mtok)    # strong model reads the result
    )
    best_pure = min(pure, key=pure.get)
    return {
        "pure": pure,
        "delegated": delegated,
        "delegation_wins": delegated < pure[ordinary[-1]],
        "cheapest_pure": best_pure,
        "note": "Delegation pays only when the returned result is much smaller "
                "than the context it was derived from. Compression ratio decides "
                "this, not task difficulty.",
    }


def _trace(*args, **kwargs) -> None:
    """Best effort: storage failing must never cost a routing decision."""
    try:
        write_trace(*args, **kwargs)
    except Exception:
        pass


def eligible_models(catalog: Mapping[str, Mapping], *, max_cost_in: float = 1e9,
                    needs_browser: bool = False) -> dict[str, Mapping]:
    """What the router may pick automatically. Shared by routing and every fallback."""
    return {
        k: v for k, v in catalog.items()
        if v["cost_in"] <= max_cost_in and (v.get("browser", True) or not needs_browser)
        and not v.get("escalation_only")
    }


def floor_model(eligible: Mapping[str, Mapping]) -> str | None:
    """Sonnet if eligible, else the strongest eligible tier, else None."""
    if not eligible:
        return None
    return FLOOR_TIER if FLOOR_TIER in eligible else max(eligible, key=lambda k: eligible[k]["tier"])


def climb_mass(catalog: Mapping[str, Mapping], floor: str, higher: str) -> float:
    """Weight on `higher` from which skipping the floor tier is cheaper in expectation."""
    return 1.0 - catalog[floor]["cost_out"] / catalog[higher]["cost_out"]


def _fallback(probabilities: Mapping[str, float] | None,
              ordinary: Mapping[str, Mapping]) -> str:
    """Floor tier, unless a higher ordinary tier carries enough weight to pay for itself."""
    probabilities = probabilities or {}
    floor = floor_model(ordinary)
    above = sorted((k for k in ordinary if ordinary[k]["tier"] > ordinary[floor]["tier"]),
                   key=lambda k: ordinary[k]["tier"], reverse=True)
    for k in above:
        if probabilities.get(k, 0.0) >= climb_mass(ordinary, floor, k):
            return k
    return floor


def route_model(
    task: str,
    *,
    catalog: Mapping[str, Mapping] = DEFAULT_CATALOG,
    needs_browser: bool = False,
    max_cost_in: float = 1e9,
    min_confidence: float = MIN_CONFIDENCE,
    trace_meta: Mapping[str, str] | None = None,
) -> dict:
    """
    Choose the cheapest eligible executor that should succeed.

    `fallback` in the result says why `selected` differs from `proposed`, or is
    None when Jev's proposal was taken. `trace_meta` is stored with the trace
    (hooks pass `tool_use_id`) so the subagent's outcome can be joined to it.
    """
    # Escalation-only tiers are never offered, so Jev's weight is spread over
    # models the router may actually pick. Capability doubt is not a reason to
    # stop work: there is no "human" candidate, only the strongest ordinary tier.
    eligible = eligible_models(catalog, max_cost_in=max_cost_in, needs_browser=needs_browser)
    if not eligible:
        decision = {"selected": None, "reason": "no eligible model", "source": "policy",
                    "fallback": "no_eligible_model"}
        _trace("model_router", {"task": task}, decision, meta=trace_meta)
        return decision

    criteria = {k: v["fit"] for k, v in eligible.items()}

    state = {
        "task": task,
        "needs_browser": needs_browser,
        "candidates": {k: {"fit": v["fit"], "relative_cost": v["cost_out"]} for k, v in eligible.items()},
        "objective": "lowest total cost that still passes acceptance",
    }
    result = decide(state, {
        "model": Choice(instructions="Which eligible model is the cheapest sufficient route?" + UNTRUSTED,
                        criteria=criteria),
        "complexity": Score(instructions="Complexity of the complete task",
                            criteria=["mechanical", "standard", "multi-step", "frontier"]),
    })

    answer = result.answers["model"]
    # An uncertain route goes to the floor tier unless a higher tier's weight
    # pays for skipping it (see FLOOR_TIER); never below the floor.
    ordinary = eligible
    proposed = str(answer.value)
    complexity = result.value("complexity")

    threshold = min_confidence
    if (
        complexity is not None and float(complexity) < MECHANICAL_COMPLEXITY
        and proposed in ordinary
    ):
        threshold = min(min_confidence, MECHANICAL_CONFIDENCE)

    fallback = None
    if answer.certainty >= threshold:
        selected = proposed
    else:
        selected = _fallback(answer.probabilities, ordinary)
        # A fallback that lands on the proposed model changed nothing: it is not
        # an escalation, so it must not be reported as one.
        fallback = None if selected == proposed else "low_confidence"
    if selected not in eligible:
        selected, fallback = _fallback(answer.probabilities, ordinary), "not_eligible"

    decision = {
        "selected": selected,
        "proposed": answer.value,
        "confidence": answer.certainty,
        "threshold": threshold,
        "complexity": complexity,
        "probabilities": answer.probabilities,
        "source": "model",
        "fallback": fallback,
        "latency_ms": getattr(result, "latency_ms", None),
        "input_tokens": getattr(result, "input_tokens", None),
        "output_tokens": getattr(result, "output_tokens", None),
    }
    _trace("model_router", state, decision, meta=trace_meta)
    return decision


if __name__ == "__main__":
    print(json.dumps(route_model("Refactor the auth service and add tests"), indent=2, default=str))
    print(json.dumps(
        estimate_costs(DEFAULT_CATALOG, context_mtok=0.65, output_mtok=0.12, tool_mtok=0.23),
        indent=2, default=str))
