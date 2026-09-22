"""
Detection for Jev's silent failure mode.

A malformed fan-out — questions that do not point at the part of the state they
are meant to judge — does not raise. It returns confident, plausible, nearly
identical answers for every item. Measured on a 188-file ranking task, the
separation between items that should score high and low collapsed from 1.91 to
0.01, and the output still looked like a ranking.

Nothing in the response distinguishes that from a real result. The only reliable
detector is to include items whose answers you already know and assert on them.
Every fan-out in this package runs canaries; treat that as mandatory, not
optional hardening.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from core import Decision


class CanaryError(AssertionError):
    """Raised when known-answer items did not come back as expected."""


@dataclass(frozen=True)
class CanaryResult:
    ok: bool
    separation: float
    positive_mean: float
    negative_mean: float
    detail: str

    def raise_if_failed(self) -> "CanaryResult":
        if not self.ok:
            raise CanaryError(self.detail)
        return self


def check_separation(
    decision: Decision,
    positive_ids: Sequence[str],
    negative_ids: Sequence[str],
    *,
    min_separation: float = 0.30,
) -> CanaryResult:
    """
    Assert the run distinguished known-relevant from known-irrelevant items.

    `positive_ids` should score high, `negative_ids` low. A separation near zero
    means the questions were not bound to their items and every answer describes
    the state as a whole.
    """
    if not positive_ids or not negative_ids:
        raise ValueError("need at least one positive and one negative canary")

    def mean(ids: Sequence[str]) -> float:
        vals = [float(decision.answers[i].value) for i in ids if i in decision.answers]
        if not vals:
            raise CanaryError(f"canary ids missing from response: {list(ids)}")
        return sum(vals) / len(vals)

    pos, neg = mean(positive_ids), mean(negative_ids)
    sep = pos - neg
    ok = sep >= min_separation

    if ok:
        detail = f"canaries ok (separation {sep:.2f})"
    else:
        detail = (
            f"CANARY FAILURE: separation {sep:.2f} < {min_separation:.2f} "
            f"(known-relevant mean {pos:.2f}, known-irrelevant mean {neg:.2f}). "
            "The usual cause is questions that do not name the state key they "
            "judge — each question sees the whole state. Results are unusable."
        )
    return CanaryResult(ok, sep, pos, neg, detail)


def check_flatness(decision: Decision, ids: Sequence[str], *, min_spread: float = 0.15) -> CanaryResult:
    """
    Secondary check for the same bug, usable without labelled canaries.

    Real relevance scores over a varied corpus spread out. A near-flat
    distribution is the signature of the binding failure. This is weaker than
    check_separation — a genuinely uniform corpus is flat for honest reasons —
    so treat a failure as a prompt to investigate, not proof.
    """
    vals = [float(decision.answers[i].value) for i in ids if i in decision.answers]
    if len(vals) < 3:
        raise ValueError("need at least three answers to judge flatness")
    spread = max(vals) - min(vals)
    ok = spread >= min_spread
    detail = (
        f"spread {spread:.2f} across {len(vals)} items"
        if ok
        else (
            f"SUSPICIOUS: spread {spread:.2f} < {min_spread:.2f} across {len(vals)} items. "
            "Either the corpus is genuinely uniform or the questions are not bound "
            "to their items. Verify with labelled canaries."
        )
    )
    mean = sum(vals) / len(vals)
    return CanaryResult(ok, spread, mean, mean, detail)


# Synthetic canary chunks for context selection. Deliberately unmistakable:
# if these two cannot be told apart, nothing subtler can be trusted either.
CANARY_RELEVANT = {
    "id": "__canary_yes",
    "text": "THIS CHUNK IS DIRECTLY AND COMPLETELY RELEVANT TO THE STATED GOAL. "
            "It contains the exact implementation the goal asks about.",
}
CANARY_IRRELEVANT = {
    "id": "__canary_no",
    "text": "This chunk is a cafeteria menu for March 1998. Tuesday: meatloaf. "
            "It has no connection to software of any kind.",
}
CANARY_IDS = (CANARY_RELEVANT["id"], CANARY_IRRELEVANT["id"])


def strip_canaries(items: Mapping[str, object]) -> dict:
    """Remove canary entries from a result before returning it to a caller."""
    return {k: v for k, v in items.items() if k not in CANARY_IDS}
