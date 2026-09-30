---
name: jev-evaluation
description: Write or debug code that calls Jev (TypeSafe's jev-latest) through this repo's core.decide — Noul, Choice and Score questions, thresholds, batching, response validation. Use when code asks Jev for a routing, classification, scoring, relevance or verification decision. For brainstorming what TypeSafe could add to a product, use typesafe-ai.
---

# Jev — structured evaluation

**Tool**: `jev_evaluate(state, questions)` on the `jev` MCP server, for decisions the
purpose-built tools do not cover. Raw HTTP details below for when you are writing the
client rather than calling it.

**Writing or changing Jev code**: also load the `typesafe-ai` skill (plugin
`typesafe@typesafe-ai`; enabled for this repo in `.claude/settings.json`, installed at user
scope by `./install.sh plugin`). Its live docs at docs.typesafe.ai are the source of truth for
primitives, confidence and cookbooks; this skill only adds what is specific to this repo's
transport and tools. Two pages worth reading before wording a question:
[models](https://docs.typesafe.ai/models.md) (limits, aliases, pinning) and
[Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md): Jev reads
literally, cannot count or compare dates, and is distracted by irrelevant state, so keep
arithmetic in code, filter state first, and turn extraction into a `Choice` over candidates.

Jev is an **evaluation model**, not a language model. Give it shared state and typed
questions; it returns choices, scores and probability distributions, all evaluated in
parallel. Reach for it when the output is a *decision*, not prose.

## Endpoint

```
POST https://openrouter.ai/api/v1/systemone        model: jev-latest
Authorization: Bearer $OPENROUTER_API_KEY
```

The default. OpenRouter serves TypeSafe's System One API unchanged — the TypeSafe SDK
reaches it with `base_url="https://openrouter.ai/api"` — and maps bare ids into its
`typesafe/` namespace (`jev-1.13` → `typesafe/jev-1.13`, `jev-latest` → `~typesafe/jev-latest`).
Three OpenRouter-only details `core.decide` handles:
- it rejects a `noul` without `instructions` (a neutral line is added on this path only);
- its response carries `usage.cost`, the USD actually billed, kept as `Decision.cost_usd` and
  summed by `hooks.py report`; TypeSafe's own API does not price the call, so there the report
  estimates at `JEV_PRICE_PER_MTOK`;
- requests carry `HTTP-Referer` / `X-OpenRouter-Title` so Jev's spend shows on its own at
  openrouter.ai/activity (the key is shared with other tooling). `402` means the OpenRouter
  account is out of credits, not a bad key.

**Pin the model once thresholds are tuned.** `jev-latest` follows TypeSafe's newest stable
release; the response's `model` field says which version answered. Thresholds here were
calibrated on jev-1.13, so set `JEV_OPENROUTER_MODEL=jev-1.13` where they must hold.

Without an OpenRouter key, `core.decide` uses `api.typesafe.ai/v1/systemone`
(`TYPESAFE_API_KEY`, same body), then the legacy Vercel AI Gateway
(`ai-gateway.vercel.sh/v1/evaluate`, model `typesafe-ai/jev`, `AI_GATEWAY_API_KEY`).
**Gateway model ids do not interchange with the other two.** Sending to
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
`chunks.f3` — say so in the instructions, naming the state path in backticks as the
TypeSafe docs do (`` `chunks` ``, `` `ticket.messages[0].text` ``).

```python
# WRONG — scores the whole corpus; answers collapse to one value
questions[cid] = Noul(instructions="Is this chunk relevant?")
# RIGHT
questions[cid] = Noul(instructions=
    f"Consider ONLY the chunk whose id is '{cid}' in `chunks`, ignoring every other "
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

Input $0.042/M, **output $0**; 64k tokens per request, of which 32k for state plus the
longest question; 255 options per Choice, 10 levels per Score; 40 requests/s and 100k
tokens/s (docs.typesafe.ai/models). **The binding ceiling is payload BYTES, not
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
