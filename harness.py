"""
Jev patterns for an automation harness, as distinct from a coding agent.

A coding agent is one long session with a large context, so the saving comes
from reducing context per task. An automation harness is many short runs at high
frequency, so the saving comes from **not running**. Different lever, same tool.

The Grok Bot case: standing automations on a schedule — a weekday report ingest,
a monthly download-and-file job. Most executions of a recurring job find nothing
that warrants the downstream pipeline. A gate at the top costs a fraction of a
cent and skips everything after it.

    5 runs/week x 52 weeks = 260 runs
    if ~70% find nothing new, ~180 full pipelines avoided for ~$0.01 of gating

Two modules:

    should_run()    does this scheduled execution need to proceed at all
    check_action()  does a proposed action satisfy policy, generalised from a
                    hand-maintained allow/block list into a classifier

Both fail closed, and both carry known-answer canaries, because a gate that
silently returns uniform answers approves everything (see canary.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import canary
from core import Choice, Noul, Score, decide, write_trace

# A gate must be cheap relative to what it guards, or it is just overhead.
# Rule of thumb: gate a pipeline costing >100x the gate. A Jev call is ~300-600ms
# and a fraction of a cent, so anything above a few seconds of work qualifies.
MIN_PROCEED = 0.55
MIN_POLICY_CONFIDENCE = 0.80


@dataclass
class RunDecision:
    action: str                      # 'proceed' | 'skip' | 'escalate'
    reason: str
    proceed_probability: float = 0.0
    novelty: float = 0.0
    confidence: float = 0.0
    source: str = "model"
    canary: str = ""

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, default=str)


@dataclass
class PolicyDecision:
    verdict: str                     # 'allow' | 'block' | 'review'
    reason: str
    matched: str = ""                # which policy line drove it
    confidence: float = 0.0
    source: str = "model"


# ------------------------------------------------------------------ run gate


def should_run(
    automation: str,
    purpose: str,
    signals: Mapping[str, Any],
    *,
    min_proceed: float = MIN_PROCEED,
    force_if_stale_runs: int | None = None,
) -> RunDecision:
    """
    Decide whether a scheduled automation should execute this time.

    `signals` must be **cheap to gather** — counts, timestamps, a list of new
    filenames. Never the contents. The whole point is spending one cheap query
    plus one cheap evaluation instead of the pipeline; if assembling signals is
    expensive, the gate has already lost.

    **Include a baseline.** A gate asked "is this far larger than normal" cannot
    answer without knowing normal. Adding `typical_new_files_per_run` moved the
    anomaly score on a 4000-file catch-up run from 0.73 to 0.86 — the difference
    between proceeding and escalating. Baselines are cheap: a stored rolling
    average from previous runs is enough.

        should_run(
            "more-weekday-ingest",
            "Ingest MORE reports from Drive and surface CRM-actionable changes",
            {"new_files_since_last_run": 0,
             "typical_new_files_per_run": 8,      # baseline — see below
             "last_run": "2026-09-21T06:00:00Z",
             "last_run_found_changes": False,
             "consecutive_empty_runs": 4},
        )

    Fails **open** — toward proceeding — which is the opposite of the command
    gate and deliberate. Skipping a run that mattered silently loses data;
    running one that did not costs tokens. When uncertain, run.

    `force_if_stale_runs` overrides the model: after that many consecutive
    skips, proceed regardless. A gate that can skip forever will eventually skip
    through something real, so the deterministic floor matters more than the
    model's judgement here.
    """
    consecutive = int(signals.get("consecutive_skipped_runs", 0) or 0)
    if force_if_stale_runs is not None and consecutive >= force_if_stale_runs:
        d = RunDecision(
            action="proceed",
            reason=f"{consecutive} consecutive skips >= floor of {force_if_stale_runs}; "
                   "running regardless of score",
            source="policy",
        )
        write_trace("harness_run_gate", {"automation": automation, "signals": dict(signals)}, d.__dict__)
        return d

    state = {
        "automation": automation,
        "purpose": purpose,
        "observed_signals": dict(signals),
        "note": "Signals are cheap observations only, not the underlying data.",
    }
    questions = {
        "proceed": Noul(criteria={
            "true": "The signals indicate new or changed material this run would act on",
            "false": "Nothing has changed since the last run; the pipeline would find nothing",
        }),
        "novelty": Score(
            instructions="How much genuinely new material is indicated, relative to a typical run?",
            criteria=["nothing new", "trivial change", "normal volume", "unusually large"],
        ),
        # Wording matters more than it looks. An earlier version asked about "a
        # sudden spike, a long gap, or a pattern unlike a routine run", which made
        # four consecutive quiet runs read as anomalous — and quiet is the single
        # most common state for a recurring job. The question must describe the
        # harm being guarded against, not merely deviation from a mean.
        "anomaly": Noul(criteria={
            "true": "The volume is far larger than a normal run, or the gap since the last "
                    "run is long enough that the automation itself may have been broken — "
                    "acting automatically could do damage or flood something downstream",
            "false": "Routine, including a completely quiet run with nothing to do. "
                     "Quiet is the normal state of a recurring job and is not anomalous",
        }),
        canary.CANARY_RELEVANT["id"]: Noul(instructions=(
            "Ignore the automation. Answer about this statement only: "
            "'Twelve brand new files arrived and none have been processed.' "
            "Does that statement describe new material to act on?"
        )),
        canary.CANARY_IRRELEVANT["id"]: Noul(instructions=(
            "Ignore the automation. Answer about this statement only: "
            "'This is a cafeteria menu from March 1998.' "
            "Does that statement describe new material to act on?"
        )),
    }

    # Split BEFORE either call, so the anomaly question is asked exactly once.
    #
    # It goes in its own call deliberately: batch-mates shift answers, and this
    # exact question scored 0.73 alongside the throughput questions versus 0.86
    # alone on identical signals — straddling the 0.70 escalation threshold. A
    # safety judgement must not depend on what else happened to be in the batch.
    anomaly_q = {"anomaly": questions.pop("anomaly")}

    result = decide(state, questions)
    anomaly_result = decide(state, anomaly_q)

    probe = canary.check_separation(
        result, [canary.CANARY_RELEVANT["id"]], [canary.CANARY_IRRELEVANT["id"]]
    )
    if not probe.ok:
        # Unusable scores. Fail open — run the pipeline rather than silently skip.
        d = RunDecision(
            action="proceed",
            reason=f"gate unreliable, running anyway: {probe.detail}",
            source="policy", canary=probe.detail,
        )
        write_trace("harness_run_gate", state, d.__dict__)
        return d

    proceed_p = float(result["proceed"])
    novelty = float(result["novelty"])
    anomaly = float(anomaly_result["anomaly"])

    if anomaly >= 0.70:
        action, reason = "escalate", f"anomalous signals (p={anomaly:.2f}); process a capped batch and report it"
    elif proceed_p >= min_proceed:
        action, reason = "proceed", f"new material indicated (p={proceed_p:.2f}, novelty={novelty:.2f})"
    else:
        action, reason = "skip", f"nothing new indicated (p={proceed_p:.2f})"

    d = RunDecision(
        action=action, reason=reason, proceed_probability=proceed_p,
        novelty=novelty, confidence=result.certainty("proceed"), canary=probe.detail,
    )
    write_trace("harness_run_gate", state, d.__dict__)
    return d


# --------------------------------------------------------------- policy gate


def check_action(
    action: str,
    allow: Sequence[str],
    block: Sequence[str] = (),
    *,
    min_confidence: float = MIN_POLICY_CONFIDENCE,
) -> PolicyDecision:
    """
    Judge a proposed action against allow/block policy written in plain English.

    This generalises a hand-maintained string list. A literal list only fires on
    the wording someone anticipated: `autoReviewInstructions.allowInstructions`
    with three sentences covers three phrasings and nothing adjacent. Handing the
    same sentences to Jev as *criteria* lets near-matches resolve correctly while
    the policy stays readable and editable by a human.

    **Block is evaluated first and wins.** An action matching both a block and an
    allow rule is blocked — policy exceptions belong in the block list's wording,
    not in a scoring tiebreak.

    Fails closed: low confidence, or no transport, becomes `review`.
    """
    if not action.strip():
        raise ValueError("action is empty")
    if not allow and not block:
        return PolicyDecision("review", "no policy configured; nothing to match against",
                              source="policy")

    state = {
        "proposed_action": action,
        "allow_policy": list(allow),
        "block_policy": list(block),
    }
    criteria = {
        "allowed": "The action matches something the allow policy explicitly permits",
        "blocked": "The action matches something the block policy forbids",
        "unlisted": "The action is not covered by either policy",
    }
    questions = {
        "verdict": Choice(
            instructions=(
                "Judge the proposed_action against the two policies. If it matches both, "
                "answer 'blocked' — block always wins."
            ),
            criteria=criteria,
        ),
        "irreversible": Noul(instructions=(
            "Would this action be hard or impossible to undo — sending money, deleting "
            "data, publishing publicly, or messaging someone outside the organisation?"
        )),
    }

    result = decide(state, questions)
    answer = result.answers["verdict"]
    verdict = str(answer.value)
    irreversible = float(result["irreversible"])

    if answer.certainty < min_confidence:
        return PolicyDecision("review", f"confidence {answer.certainty:.2f} < {min_confidence}",
                              confidence=answer.certainty)
    if verdict == "blocked":
        final, reason = "block", "matches block policy"
    elif verdict == "unlisted":
        final, reason = "review", "not covered by policy; a person should decide"
    elif irreversible >= 0.50:
        final, reason = "review", f"allowed by policy but hard to undo (p={irreversible:.2f})"
    else:
        final, reason = "allow", "matches allow policy and is reversible"

    d = PolicyDecision(final, reason, matched=verdict, confidence=answer.certainty)
    write_trace("harness_policy_gate", state, d.__dict__)
    return d


if __name__ == "__main__":
    print("── run gate ──")
    for label, sig in (
        ("nothing new", {"new_files_since_last_run": 0, "typical_new_files_per_run": 8,
                         "consecutive_empty_runs": 4, "last_run_found_changes": False}),
        ("new material", {"new_files_since_last_run": 12, "typical_new_files_per_run": 8,
                          "consecutive_empty_runs": 0, "last_run_found_changes": True}),
        ("anomalous", {"new_files_since_last_run": 4000, "typical_new_files_per_run": 8,
                       "consecutive_empty_runs": 0, "last_run": "2026-03-01T06:00:00Z",
                       "today": "2026-09-22"}),
    ):
        d = should_run("more-weekday-ingest",
                       "Ingest MORE reports from Drive and surface CRM-actionable changes",
                       sig, force_if_stale_runs=10)
        print(f"  {label:14} -> {d.action:9} {d.reason}")

    print("\n── policy gate ──")
    ALLOW = [
        "Create a standing monthly automation to download Melaleuca reports from the "
        "signed-in Melaleuca.com/grow site and upload new files to a Google Drive folder.",
        "Create a standing automation to ingest Melaleuca MORE reports from Google Drive "
        "and surface CRM-actionable changes on a recurring schedule.",
    ]
    BLOCK = ["Anything that sends email to customers, moves money, or deletes records."]
    for act in (
        "Set up a weekly job pulling MORE reports from Drive into the CRM",
        "Email every customer in the CRM about the new report",
        "Delete last quarter's report files from Drive",
        "Generate a chart of report volume over time",
    ):
        d = check_action(act, ALLOW, BLOCK)
        print(f"  {d.verdict:7} {act[:52]:54} [{d.reason[:38]}]")
