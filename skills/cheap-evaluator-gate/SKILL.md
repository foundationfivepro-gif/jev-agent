---
name: cheap-evaluator-gate
description: Decide whether putting a cheap evaluator in front of expensive work pays off, and how to gate it — thresholds, batching, gating on the bad outcome's probability, verifying a compression kept key facts, gating risky actions. Use when designing a gate or checking what one costs. For choosing which files or chunks enter context, use context-tiering.
---

# Gating expensive work behind a cheap evaluator

Before spending a lot of tokens, spend a few deciding whether to.

**Where it pays.** Relevance-score before reading (the highest-leverage gate — reading is the
largest consumer). Verify a compression kept the facts the next step needs. Route among many
tools without putting every schema in context. Score a proposed command against policy.

**Batch, because only input is billed.** All questions in one request run in parallel against
shared state. Score 40 files in one call with 40 questions, not 40 calls.

**Where it does NOT pay.** The gated work is cheap. You need the content anyway. The judgement
needs real reasoning — these models make fast structured calls, not careful arguments. The
gate's own state would be huge — score over paths and signatures, not full contents.

**Use the probabilities.** Booleans return probabilities, not verdicts. Set the threshold by
the cost of being wrong: cheap to over-include, threshold low; destructive, threshold high and
escalate the uncertain middle to a real model or a human rather than guessing.

**Gate on the mass of the bad outcome, not on confidence.** A safe command with P(block)=0 and
confidence 0.69 fails a 0.90 confidence gate — and a gate that fires on safe input is a gate
people switch off. The converse also holds: a model's "review" label on plain local work is
not risk; overrule it when P(block) is near zero and nothing leaves the machine.

**Relay a review honestly.** When a gate returns `review`, give its reason and the exact
command, ask once for a batch of related commands, and never claim it flagged "every" command
without checking.
