"""
Tool and MCP router: load one tool's schema instead of all of them.

Two rules make this safe. The catalog is filtered in code before Jev sees it, so
a disabled or unpermitted tool cannot be selected even in principle; and "none"
is always an option, so the model is never forced to pick a tool it does not
believe in. Low confidence collapses to "none" rather than to a guess.

The catalog must be rebuilt after any action that could change it. A tool
selected from a stale catalog is the same bug class as a diff applied to a stale
snapshot.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from core import Choice, decide, write_trace

# On the chosen tool's probability, not confidence: the live tool list changes
# size, and a confidence bar would mean something different for each size.
# 0.85 is what the former 0.80 confidence bar demanded among 3-5 options.
MIN_PROBABILITY = 0.85


def route_tool(
    goal: str,
    catalog: Mapping[str, Mapping[str, Any]],
    *,
    allowed_scopes: Sequence[str] | None = None,
    min_probability: float = MIN_PROBABILITY,
) -> dict:
    """
    Pick at most one tool for `goal`.

    `catalog` maps tool id -> {summary, schema, enabled, scopes}. Only enabled
    tools whose scopes are a subset of `allowed_scopes` are offered.
    """
    live = {}
    for tid, meta in catalog.items():
        if not meta.get("enabled", True):
            continue
        scopes = set(meta.get("scopes", []))
        if allowed_scopes is not None and not scopes.issubset(set(allowed_scopes)):
            continue
        live[tid] = meta

    if not live:
        decision = {"selected": "none", "reason": "no live tools after filtering", "source": "policy"}
        write_trace("tool_router", {"goal": goal}, decision)
        return decision

    criteria = {tid: str(meta.get("summary", tid)) for tid, meta in live.items()}
    criteria["none"] = "No listed tool is required for this goal"

    state = {"goal": goal, "available_tools": sorted(live)}
    result = decide(state, {
        "tool": Choice(instructions="Which single tool should run next?", criteria=criteria)
    })
    answer = result.answers["tool"]
    selected = str(answer.value) if answer.chosen_probability >= min_probability else "none"

    # Never trust a name back from the model without checking it against the
    # live catalog — a hallucinated id must not reach the executor.
    if selected not in live and selected != "none":
        selected = "none"

    decision = {
        "selected": selected,
        "proposed": answer.value,
        "confidence": answer.certainty,
        "probability": answer.chosen_probability,
        "schema": live.get(selected, {}).get("schema"),
        "probabilities": answer.probabilities,
        "source": "model",
    }
    write_trace("tool_router", state, decision)
    return decision


DEMO_CATALOG = {
    "search_code": {"summary": "Read-only repository search", "schema": {"query": "str"},
                    "enabled": True, "scopes": ["read"]},
    "run_tests": {"summary": "Run the project test suite", "schema": {"path": "str"},
                  "enabled": True, "scopes": ["read", "exec"]},
    "browser": {"summary": "Open a public URL", "schema": {"url": "str"},
                "enabled": False, "scopes": ["net"]},
}

if __name__ == "__main__":
    print(json.dumps(route_tool("Find the authentication tests", DEMO_CATALOG), indent=2, default=str))
