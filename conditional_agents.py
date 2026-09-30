"""
Conditional AGENTS.md: load the rules this task needs, not one permanent prompt.

Two filters in order. Path globs run in code and cost nothing — if no rule
matches the touched files, there is no API call at all. Jev then decides which
of the surviving rules actually constrain *this* task.

Mandatory rules bypass the model entirely. A rule that must always hold cannot
be subject to a semantic filter that might drop it, so `always` rules are
injected deterministically and are not scored. Rules grant nothing: they
constrain behaviour and can never widen permissions, which stay with
permission_gate and security_router.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from typing import Sequence

import canary
from core import Noul, decide_batched, write_trace

MIN_RELEVANCE = 0.60


@dataclass
class Rule:
    id: str
    paths: list[str]
    text: str
    always: bool = False          # injected without scoring
    tags: list[str] = field(default_factory=list)


DEFAULT_RULES = [
    Rule("security", ["**/auth/**", "**/security/**", "**/*secret*"],
         "Never log secrets or weaken authorization checks.", always=True),
    Rule("python", ["**/*.py"], "Run pytest for changed Python code."),
    Rule("frontend", ["web/**", "**/*.tsx", "**/*.css"],
         "Preserve keyboard access and responsive layout."),
    Rule("migrations", ["**/migrations/**"],
         "Migrations must be reversible and reviewed before merge."),
]


def _matches(path: str, pattern: str) -> bool:
    return (
        fnmatch.fnmatch(path, pattern)
        or fnmatch.fnmatch("/" + path, pattern)
        or fnmatch.fnmatch(path, pattern.replace("**/", ""))
    )


def _question(rule_id: str) -> Noul:
    """Scoped to one rule by name — see context_tier._question."""
    return Noul(
        instructions=(
            f"Consider ONLY the rule whose id is '{rule_id}' in `rules`, "
            f"ignoring every other rule. Must that rule constrain how this task "
            f"is carried out?"
        )
    )


def select_rules(
    goal: str,
    files: Sequence[str],
    rules: Sequence[Rule] = DEFAULT_RULES,
    *,
    min_relevance: float = MIN_RELEVANCE,
) -> dict:
    """Return the rule ids and instruction text that should constrain this task."""
    candidates = [r for r in rules if any(_matches(f, p) for f in files for p in r.paths)]
    if not candidates:
        payload = {"rule_ids": [], "instructions": [], "always": [], "source": "policy"}
        write_trace("conditional_agents", {"goal": goal, "files": list(files)}, payload)
        return payload

    mandatory = [r for r in candidates if r.always]
    scorable = [r for r in candidates if not r.always]

    active = list(mandatory)
    scores: dict[str, float] = {}
    canary_detail = "not run (no scorable rules)"

    if scorable:
        state_rules = {r.id: {"text": r.text, "paths": r.paths} for r in scorable}
        questions = {r.id: _question(r.id) for r in scorable}
        for probe, text in (
            (canary.CANARY_RELEVANT["id"], "This rule governs exactly the work described in the goal."),
            (canary.CANARY_IRRELEVANT["id"], "Cafeteria menu policy for March 1998. Tuesday: meatloaf."),
        ):
            state_rules[probe] = {"text": text, "paths": []}
            questions[probe] = _question(probe)

        def state_for(ids):
            keep = set(ids) | set(canary.CANARY_IDS)
            return {"goal": goal, "files": list(files),
                    "rules": {k: v for k, v in state_rules.items() if k in keep}}

        result = decide_batched(state_for, questions)
        probe_result = canary.check_separation(
            result, [canary.CANARY_RELEVANT["id"]], [canary.CANARY_IRRELEVANT["id"]]
        ).raise_if_failed()
        canary_detail = probe_result.detail

        scores = {r.id: float(result.value(r.id, 0.0)) for r in scorable}
        active += [r for r in scorable if scores[r.id] >= min_relevance]

    payload = {
        "rule_ids": [r.id for r in active],
        "instructions": [r.text for r in active],
        "always": [r.id for r in mandatory],
        "scores": scores,
        "canary": canary_detail,
        "source": "model" if scorable else "policy",
    }
    write_trace("conditional_agents", {"goal": goal, "files": list(files)}, payload)
    return payload


if __name__ == "__main__":
    print(json.dumps(select_rules("Fix the login redirect", ["src/auth/login.py"]), indent=2, default=str))
