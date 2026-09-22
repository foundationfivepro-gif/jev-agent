---
name: automation-run-gating
description: Decide whether a scheduled or recurring automation needs to run at all, and whether a proposed action satisfies policy. Use when building bots, cron jobs, ingest pipelines, or any harness that executes on a schedule rather than on demand.
---

# Gating recurring automations

For an agent harness the economics differ from a coding agent. A coding agent is
one long session with a large context, so the saving is reducing context per
task. A harness is many short runs at high frequency, so the saving is **not
running at all**.

A weekday job where ~70% of executions find nothing wastes ~180 full pipelines a
year. A gate at the top costs a fraction of a cent and skips them.

## The tools

Exposed by the `jev` MCP server, and as plain functions in `harness.py`:

| pattern | MCP tool | function |
|---|---|---|
| run gate | `jev_should_run(automation, purpose, signals, force_after_skips)` | `should_run()` |
| policy gate | `jev_check_action(action, allow, block)` | `check_action()` |

## Two gates, failing in opposite directions

| | run gate | policy gate |
|---|---|---|
| decides | proceed / skip / escalate | allow / block / review |
| fails | **open** — when unsure, run | **closed** — when unsure, review |
| why | skipping a real run loses data silently; a redundant run only costs tokens | acting wrongly is worse than asking |

The asymmetry is the design. Copy it rather than picking one default for both.

## Signals must be cheap, and must include a baseline

Pass counts, timestamps, filenames — never the underlying data. If gathering the
signals is expensive the gate has already lost.

**Include a baseline.** "Is this far larger than normal" cannot be answered
without knowing normal. On a 4000-file catch-up run, adding a typical-volume
signal moved the anomaly score from 0.73 to 0.86 — across the escalation
threshold, so the difference between flooding a CRM and asking a human. A stored
rolling average from previous runs is enough.

## Word the question around the harm, not around deviation

An anomaly question phrased as "a spike, a long gap, or a pattern unlike a
routine run" made four consecutive quiet runs read as anomalous. **Quiet is the
normal state of a recurring job.** Describe what you are guarding against —
volume large enough to do damage downstream, or a gap long enough that the
automation itself may be broken — not statistical deviation.

## Ask safety questions in their own call

Batch-mates shift answers. The same anomaly question scored 0.73 alongside the
throughput questions and 0.86 alone on identical signals. A judgement that gates
automated action should not depend on what else happened to be in the batch.
Batch the cheap throughput questions; isolate the safety one.

## Put a deterministic floor under the gate

A gate that can skip forever will eventually skip through something real. Force a
run after N consecutive skips, in code, regardless of the score. The floor
matters more than the model's judgement here.

## Policy as criteria, not as a string list

A hand-maintained list of permitted phrasings only fires on the wording someone
anticipated. Handing the same sentences to an evaluator as *criteria* lets
adjacent cases resolve while the policy stays readable and human-editable.

Evaluate **block first, and let it win** — an action matching both lists is
blocked. Policy exceptions belong in the block list's wording, never in a scoring
tiebreak. Anything matching neither list is `review`, never a silent allow.
