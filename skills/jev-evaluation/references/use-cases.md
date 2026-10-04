# Jev use cases: one recipe, many piles

Almost every Jev use case is the same move: **point Jev at a pile of records you already
have and have it label, pick or score each one.** Build that recipe once; the use cases
below differ only in the pile, the label set and what happens downstream.

Speeds and costs quoted here are third-party claims (RoboNuggets, "19 Jev-Claude use
cases", Oct 2026), not measurements from this repo. Re-measure before quoting them.

## The recipe

1. **Fixed label set.** A `Choice` with `{label: description}`. Jev can only return a
   key you listed, so it never invents a category. Add an explicit `"other"` or `"none"`
   so it is never forced into a wrong bucket.
2. **One question per record, bound by id.** See "Question ids do NOT bind to state keys"
   in SKILL.md. Every instruction names the record it judges.
3. **Slice state per batch.** `decide_batched(lambda ids: {...only those records...}, qs)`.
4. **Route on certainty, not on the label alone.** Below the threshold, a person decides.
   Start near 0.6 and move it as the validation set (step 5) shows where Jev is wrong.
5. **Validation set before trusting it.** A few dozen to a few hundred records whose right
   label you already know. Run Jev blind, count agreement, read the misses: the misses
   are where label descriptions need tightening. For `Noul`/`Score` fan-outs also run
   `canary.check_separation` on known-high and known-low items.
6. **Jev picks; something else acts.** Jev's output is a label. Anything with external
   effect (send, post, tag in a shared system) goes through `jev_check_action` or a human.

```python
from core import Choice, decide_batched

LABELS = {
    "ready_to_buy": "Asks price, where to buy, or says they want it",
    "question":     "Asks about the product without buying intent",
    "complaint":    "Reports a problem or is unhappy",
    "other":        "Anything else, including spam and small talk",
}
records = {"c1": "how much is the starter kit?", "c2": "love this!!"}

qs = {rid: Choice(criteria=LABELS, instructions=(
        f"Classify ONLY the record whose id is '{rid}' in state.records."))
      for rid in records}
d = decide_batched(lambda ids: {"records": {i: records[i] for i in ids}}, qs)

auto   = {rid: d[rid] for rid in records if d.certainty(rid) >= 0.6}
review = [rid for rid in records if d.certainty(rid) < 0.6]

# Validation: run the same code on records with known labels.
known = {"c1": "ready_to_buy", "c2": "other"}
accuracy = sum(d.value(r) == lab for r, lab in known.items()) / len(known)
```

## Catalogue

Grouped by how they plug in. "Built" means a module or tool in this repo already does it.

### Sort a pile you already have
| Use case | Pile | Labels or score |
|---|---|---|
| Spreadsheet category column | rows of a sheet (e.g. bank export) | the user's category list |
| Inquiry triage | support messages | billing / bug / question / other + certainty → human below threshold |
| Competitor ad tagging | Meta Ad Library ad texts | format, call to action, funnel stage |
| Clip finding | transcript windows of a long video | Score: worth posting; keep the top N |
| Customer health | customer rows (tenure, plan, cancel signals) | healthy / needs attention / churn risk |
| Buyers in comments | scraped social comments | ready to buy / question / complaint / other |
| Inbox in front of an agent | emails | needs reply / deal / spam-scam / FYI; only "needs reply" reaches the LLM |

For the Wellness by Madeline CRM versions of the last four, use the `jev-crm-triage` skill.

### Route the agent (built)
| Use case | Where |
|---|---|
| Pick the skill | `jev_route_skill`, `skill_router.py`, skill `skill-routing` |
| Pick the model | `jev_route_model`, `model_router.py`, the agent-spawn hook |
| Skip work that need not run | `jev_should_run`, skill `automation-run-gating` |
| Pick the files to read | `jev_select_context`, skill `context-tiering` |

### Search and link by meaning
| Use case | Shape |
|---|---|
| Find-in-page by meaning | split into paragraphs; `Choice` over paragraph ids (+ `"none"`); scroll to the pick |
| Image search | needs text per image (generation prompt, or a caption from a small vision model); then the same `Choice` |
| Interlinking pages or notes | per page, `Noul` per candidate target: "should page A link to page B?"; canaries mandatory |
| Narrow Q&A without an LLM | `Choice` over a fixed list of documents or answers, each described in one line |

### Real-time UI (Jev answers in well under a second)
| Use case | Shape |
|---|---|
| Live sentence tagging (calls, debates, meetings) | per sentence: decision / action item / risk / question / other |
| Feed filter extension | per post: `Noul` "is this low-effort AI filler?"; fold the true ones |
| Icon or component picking | `Choice` over the icon or component library as the user types |
| Pages that assemble themselves | `Choice` per slot over ready-made parts; Jev picks, never writes |

For real-time paths decide the no-answer default first (see "When Jev is silent"):
the UI must still render when Jev does not answer in time.

## Finding more use cases

Two prompts that work:

- "Based on what you know about this business, which of these Jev use cases would help
  most?" with a list such as shipwithjev.com attached. For product ideas, load the
  `typesafe-ai` plugin skill.
- "Look back over my recent sessions and point out the steps where a yes/no, pick-one or
  score call would have done the job instead of an LLM." The `jev-chief-of-staff` skill
  covers moving those decisions onto Jev.
