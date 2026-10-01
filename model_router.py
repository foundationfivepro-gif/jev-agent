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
import math
import os
import re
from typing import Mapping

from core import UNTRUSTED, Choice, Score, decide, write_trace

MIN_CONFIDENCE = 0.75

# Below this complexity a task is mechanical, and a cheap-tier proposal is
# accepted at the lower bar: a Haiku retry on a one-line edit costs almost
# nothing, so failing toward capability there only spends Opus for no reason.
MECHANICAL_COMPLEXITY = 0.5
MECHANICAL_CONFIDENCE = 0.5

# An uncertain route falls back to the strongest tier Jev gave real weight to,
# not blindly to the top: a sonnet-vs-haiku split never needed Opus. Weight on a
# tier outside the ordinary set (escalation-only, or human) still means the
# strongest ordinary tier — that mass is a signal the task is hard.
MIN_FALLBACK_MASS = 0.2

# cost_in / cost_out are USD per million tokens, Anthropic first-party rates.
# Keys are the names Claude Code's Agent tool accepts for its `model` parameter.
# Fable is the top of the range, not a cheap tier: it costs twice Opus. It is
# escalation_only — Jev may propose it with confidence, but an *uncertain* route
# falls back to an ordinary tier (at most Opus), never up to Fable.
DEFAULT_CATALOG: dict[str, dict] = {
    "haiku":  {"id": "claude-haiku-4-5",
               "fit": "Classification, formatting, simple mechanical edits, "
                      "search-and-report subtasks that return a short answer",
               "cost_in": 1.0, "cost_out": 5.0, "tier": 1},
    "sonnet": {"id": "claude-sonnet-5",
               "fit": "Normal coding, research, multi-file edits, "
                      "most day-to-day engineering",
               "cost_in": 2.0, "cost_out": 10.0, "tier": 2},
    "opus":   {"id": "claude-opus-5",
               "fit": "Complex architecture, hard debugging, subtle refactors, "
                      "work where a wrong answer is expensive to detect",
               "cost_in": 5.0, "cost_out": 25.0, "tier": 3},
    "fable":  {"id": "claude-fable-5-1",
               "fit": "Frontier complexity: long-horizon architecture, system design "
                      "and complex multi-page web design where a wrong structural "
                      "decision is expensive to unwind. Never routine tasks, and not "
                      "for ordinary multi-step work that Opus handles",
               "cost_in": 10.0, "cost_out": 50.0, "tier": 4, "escalation_only": True},
}

def available_catalog(catalog: Mapping[str, Mapping] = DEFAULT_CATALOG) -> dict[str, dict]:
    """
    The menu as it stands now, not as it was written: only the models listed in
    JEV_MODELS (comma-separated, e.g. "haiku,sonnet"), when it is set. A router
    that can pick a model the account cannot run is choosing from yesterday's menu.
    """
    wanted = {m.strip().lower() for m in os.environ.get("JEV_MODELS", "").split(",") if m.strip()}
    menu = {k: dict(v) for k, v in catalog.items() if not wanted or k in wanted}
    # Empty is evidence of no eligible model, never permission to restore models
    # the operator excluded (including a misspelled allowlist).
    return menu


_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "persistent"})
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/~-]{0,199}$")


def _finite_nonnegative(value) -> bool:
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value) and value >= 0)
    except OverflowError:
        return False


def validate_catalog(catalog: Mapping[str, Mapping]) -> None:
    """Validate all candidates before filtering or constructing outbound state."""
    if not isinstance(catalog, Mapping) or len(catalog) > 64:
        raise ValueError("invalid model catalog")
    for name, model in catalog.items():
        if (not isinstance(name, str) or not _MODEL_NAME.fullmatch(name) or name == "human"
                or not isinstance(model, Mapping)):
            raise ValueError("invalid model candidate")
        if not isinstance(model.get("fit"), str) or not 0 < len(model["fit"]) <= 4000:
            raise ValueError("invalid model fit")
        if any(not _finite_nonnegative(model.get(cost)) for cost in ("cost_in", "cost_out")):
            raise ValueError("model costs must be finite and nonnegative")
        if isinstance(model.get("tier"), bool) or not isinstance(model.get("tier"), int) or model["tier"] < 1:
            raise ValueError("model tier must be a positive integer")
        if "id" in model and (not isinstance(model["id"], str) or not _MODEL_NAME.fullmatch(model["id"])):
            raise ValueError("invalid executable model ID")
        for flag in ("browser", "escalation_only"):
            if flag in model and not isinstance(model[flag], bool):
                raise ValueError("invalid model control")
        if "efforts" in model:
            efforts = model["efforts"]
            if (not isinstance(efforts, (list, tuple)) or len(efforts) > len(_EFFORTS)
                    or any(not isinstance(e, str) or e not in _EFFORTS for e in efforts)
                    or len(set(efforts)) != len(efforts)):
                raise ValueError("invalid model efforts")


def _failure(status: str) -> dict:
    # Keep the legacy safe sentinel while exposing the machine-readable reason.
    return {"selected": "human", "reason": status.replace("_", " "), "source": "policy",
            "fallback": status, "status": status, "failure": status}


def estimate_costs(
    catalog: Mapping[str, Mapping], *, context_mtok: float, output_mtok: float, tool_mtok: float
) -> dict:
    """
    Compare doing the whole task on each model against delegating generation to
    the cheapest one and having the strongest absorb the result.

    Returns per-model totals plus the delegated total, so a caller can see
    whether routing actually pays at *their* prices and context size.
    """
    validate_catalog(catalog)
    if not catalog:
        return {"status": "no_eligible_model", "failure": "no_eligible_model", "pure": {},
                "delegated": None, "delegation_wins": False, "cheapest_pure": None}
    if any(not _finite_nonnegative(v) for v in (context_mtok, output_mtok, tool_mtok)):
        raise ValueError("token volumes must be finite and nonnegative")
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
        + strong["cost_in"] * (output_mtok + tool_mtok)   # strong model re-reads the result
    )
    if not math.isfinite(delegated) or any(not math.isfinite(cost) for cost in pure.values()):
        raise ValueError("estimated cost overflow")
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


def _fallback(probabilities: Mapping[str, float] | None,
              ordinary: Mapping[str, Mapping], strongest: str) -> str:
    contenders = [k for k, p in (probabilities or {}).items() if p >= MIN_FALLBACK_MASS]
    if not contenders or any(k not in ordinary for k in contenders):
        return strongest
    return max(contenders, key=lambda k: ordinary[k]["tier"])


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
    try:
        validate_catalog(catalog)
    except (ValueError, TypeError):
        return _failure("invalid_catalog")
    if (not _finite_nonnegative(max_cost_in) or not _finite_nonnegative(min_confidence)
            or min_confidence > 1 or not isinstance(needs_browser, bool)):
        return _failure("invalid_request")
    eligible = {
        k: v for k, v in catalog.items()
        if v["cost_in"] <= max_cost_in and (v.get("browser", True) or not needs_browser)
    }
    if not eligible:
        decision = _failure("no_eligible_model")
        write_trace("model_router", {"task": task}, decision, meta=trace_meta)
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
        "model": Choice(instructions="Which eligible model is the cheapest sufficient route?" + UNTRUSTED,
                        criteria=criteria),
        "complexity": Score(instructions="Complexity of the complete task",
                            criteria=["mechanical", "standard", "multi-step", "frontier"]),
    })

    try:
        answer = result.answers["model"]
        proposed = answer.value
        complexity = result.value("complexity")
        if (not isinstance(proposed, str) or proposed not in criteria
                or not _finite_nonnegative(answer.certainty) or answer.certainty > 1
                or (complexity is not None and
                    (not _finite_nonnegative(complexity) or complexity > 3))):
            return _failure("invalid_response")
        probabilities = answer.probabilities
        if probabilities is not None:
            if not isinstance(probabilities, Mapping):
                return _failure("invalid_response")
            if any(k not in criteria or not _finite_nonnegative(p) or p > 1
                   for k, p in probabilities.items()):
                return _failure("invalid_response")
            if probabilities and not math.isclose(sum(probabilities.values()), 1, abs_tol=1e-3):
                return _failure("invalid_response")
            if probabilities and (proposed not in probabilities or
                                  probabilities[proposed] < max(probabilities.values())):
                return _failure("invalid_response")
        # Legacy requests do not offer an effort question. If an injected or
        # changed decision source nevertheless returns one, validate it rather
        # than quietly forwarding an unsupported executor parameter.
        effort_answer = result.answers.get("effort")
        if effort_answer is not None:
            effort = effort_answer.value
            if (not isinstance(effort, str) or effort not in _EFFORTS or proposed not in eligible
                    or effort not in eligible[proposed].get("efforts", ())):
                return _failure("invalid_response")
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return _failure("invalid_response")
    # Fail closed toward capability: an uncertain route goes to the strongest
    # tier Jev seriously considered (see MIN_FALLBACK_MASS), because a cheap
    # failure costs the cheap attempt plus the expensive retry plus the latency
    # of noticing. Escalation-only tiers are excluded from that fallback —
    # uncertainty is not a reason to pay double.
    ordinary = {k: v for k, v in eligible.items() if not v.get("escalation_only")} or eligible
    strongest = max(ordinary, key=lambda k: ordinary[k]["tier"])

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
        selected = _fallback(answer.probabilities, ordinary, strongest)
        fallback = "low_confidence"
    if selected not in eligible and selected != "human":
        selected, fallback = strongest, "not_eligible"

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
    write_trace("model_router", state, decision, meta=trace_meta)
    return decision


if __name__ == "__main__":
    print(json.dumps(route_model("Refactor the auth service and add tests"), indent=2, default=str))
    print(json.dumps(
        estimate_costs(DEFAULT_CATALOG, context_mtok=0.65, output_mtok=0.12, tool_mtok=0.23),
        indent=2, default=str))
