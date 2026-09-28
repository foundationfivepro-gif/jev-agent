---
name: jev-evaluation
description: Write or debug code that calls Jev (TypeSafe's jev-latest) through this repo's core.decide — Noul, Choice and Score questions, thresholds, batching, response validation. Use when code asks Jev for a routing, classification, scoring, relevance or verification decision. For brainstorming what TypeSafe could add to a product, use typesafe-ai.
---

# Jev — structured evaluation

**Tool**: `jev_evaluate(state, questions)` on the `jev` MCP server, for decisions the
purpose-built tools do not cover. Raw HTTP details below for when you are writing the
client rather than calling it.

**Writing or changing Jev code**: also load the `typesafe-ai` skill (plugin
`typesafe@typesafe-ai`, enabled in this repo's `.claude/settings.json`). Its live docs at
docs.typesafe.ai are the source of truth for primitives, confidence and cookbooks.

Jev is an **evaluation model**, not a language model. Give it shared state and typed
questions; it returns choices, scores and probability distributions, all evaluated in
parallel. Reach for it when the output is a *decision*, not prose.

## Endpoint

```
POST https://api.typesafe.ai/v1/systemone          model: jev-latest
Authorization: Bearer $TYPESAFE_API_KEY
```

`core.decide` uses this whenever `TYPESAFE_API_KEY` is set. Legacy fallback: the Vercel AI
Gateway (`ai-gateway.vercel.sh/v1/evaluate`, model `typesafe-ai/jev`, `AI_GATEWAY_API_KEY`).
OpenRouter uses `typesafe/jev-latest`. **The namespaces do not interchange.** Sending to
`/v1/chat/completions` returns `ModelTypeMismatchError`.

## Request

```json
{"model": "jev-latest",
 "state": {"ticket": "Checkout 500s when applying a coupon."},
 "questions": {
   "is_bug":   {"type": "noul",    "instructions": "Is this a defect?"},
   "severity": {"type": "score",   "criteria": ["trivial","minor","major","critical"]},
   "team":     {"type": "choice",  "criteria": {"payments": "Billing", "infra": "Servers"}}}}
```

`questions` is a **record keyed by id**, not an array. Each needs `criteria` or
`instructions`. TypeSafe and the SDK call the yes/no type `noul` (answer field `noul`); the legacy gateway
calls it `boolean` (answer field `probability`).

## ⚠ Question ids do NOT bind to state keys

Every question is evaluated against the **entire state**. Id `f3` has no implicit link to
`state.chunks.f3` — say so in the instructions.

```python
# WRONG — scores the whole corpus; answers collapse to one value
questions[cid] = Noul(instructions="Is this chunk relevant?")
# RIGHT
questions[cid] = Noul(instructions=
    f"Consider ONLY the chunk whose id is '{cid}' in state.chunks, ignoring every other "
    f"chunk. Could it materially change the answer to the goal?")
```

Measured on 188 files: implicit binding gave separation **0.01** (0% recall); explicit gave
**1.91** (88%). It fails **silently** — confident, plausible, uniformly wrong. Put
known-answer canaries in every fan-out and assert they separate.

## Types

| type | criteria | returns |
|---|---|---|
| `boolean`/`noul` | `{true,false}` or use `instructions` | `{probability}` — **no confidence field** |
| `score` | ordered `str[]` low→high | `{score, probabilities, confidence}` |
| `choice` | `{option: description}` | `{choice, probabilities, confidence}` |

Booleans return a probability, not a verdict; derive certainty as `abs(p-0.5)*2` and set the
threshold at the call site. **Gate on the probability mass of bad outcomes, not confidence** —
a safe command scored P(block)=0 with confidence 0.69, and a 0.90 confidence gate wrongly sent
it to review. Gates that fire on safe input get switched off.

## Batching

Input ~$0.042/M, **output $0**, 32k context. **The binding ceiling is payload BYTES, not
question count.** ~150 questions work in isolation, 170+ hard-fails; but enriching per-item
state made batches of 50 start failing that worked at 50. Pack to ~24k chars AND ~50
questions, and slice state so each batch carries only its own items.

**Scores are comparable only within a batch** — batch-mates shift each answer; the same corpus
split differently moved recall 7/8 → 6/8. Re-score top candidates together before deciding.

Errors: gateway 400/503, direct 422/529. A 429 carries `Retry-After: 60`; a backoff capped
below that burns the budget without ever waiting long enough — and one that honours it
stalls an agent for a minute. `decide()` gives up at `JEV_DEADLINE_S` (15s) instead;
raise it for offline batch jobs that can afford to wait.

## When Jev is silent

No answer at all is a normal outcome, and a checker with no branch for it stops the
pipeline. Decide the no-answer default before writing the
call, and pick it from what being wrong costs:

| the decision | no answer means | tool behaviour |
|---|---|---|
| irreversible action | human review | `jev_check_action` fails closed |
| outbound command | the user's permission mode | `jev_gate_command` returns `review`; hook notes it |
| scheduled run | proceed | `jev_should_run` fails open |
| model route | unset; session default | `jev_route_model` error names it |
| which files to read | Grep/Glob, read the matches | `jev_select_context` error names it |
| your own `jev_evaluate` | the default you chose | error says so |

Every "Jev unreachable" error ends with `Fallback:`. Apply it and continue. Never read
silence as yes, and never as a reason to stop.
