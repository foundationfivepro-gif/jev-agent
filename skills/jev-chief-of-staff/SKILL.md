---
name: jev-chief-of-staff
description: Run a Chief-of-Staff agent's small decisions (which worker acts next, is this source relevant, is the draft ready, does this need approval) on TypeSafe's Jev instead of a frontier LLM. Use when building or tuning a dispatcher that hands work between research, writing and review agents, or when an agent spends LLM calls on yes/no, route or score judgements.
---

# Jev as the Chief of Staff's decision layer

An LLM creates the work, Jev decides what happens next, and code carries the decision out.

A Chief of Staff agent spends most of its turns on small forks: choose a worker, check a
result, decide whether to continue. Each fork is usually a frontier-model call made before
any useful work happens. Jev is a structured evaluation model. You give it state and typed
questions, and it returns choices, scores and probabilities. It cannot write, plan or
explain, so leave those to the LLM.

| belongs to | examples |
|---|---|
| **the LLM** | research, plan, write, fill a form field |
| **Jev** | route, score, approve, escalate |
| **code** | exact rules (stop after 10 actions, spend cap), execution, saving files |

## 1. Find the decisions

Take a real job, such as *"Research three new AI-agent tools and draft tomorrow's briefing.
Save the draft for my review."* The forks in it are:
- Do we have enough sources?
- Which worker goes next?
- Is the draft ready for review?

Each of these is a Jev question. Fetching, writing and saving are not. Start with the one
decision that repeats most often, measure it, then replace the next one.

## 2. Ask it

OpenRouter (one key, `OPENROUTER_API_KEY`; TypeSafe's own API at `api.typesafe.ai/v1/systemone`
takes the same body with `TYPESAFE_API_KEY`):

```
POST https://openrouter.ai/api/v1/systemone       model: jev-latest
```

The direct API is `typesafe-sdk` with model `jev-latest`, and OpenRouter uses
`typesafe/jev-latest`. Model ids are not interchangeable between the three.

```json
{"model": "typesafe-ai/jev",
 "state": {
   "request": "Compare three AI-agent tools for tomorrow's briefing",
   "sources": [{"id": "s1", "title": "...", "summary": "..."}],
   "gaps": ["no pricing for tool C"],
   "draft": null},
 "questions": {
   "next_worker": {"type": "choice", "instructions": "Which worker should act next?",
     "criteria": {
       "research": "Evidence is missing or a listed gap is unfilled",
       "write":    "Evidence is sufficient and no draft exists yet",
       "review":   "The request is unclear, or a complete draft exists"}},
   "urgency": {"type": "score", "criteria": ["can wait", "today", "now"]},
   "needs_publishing": {"type": "boolean",
     "instructions": "Does completing state.request require publishing or sending anything?"}}}
```

| type | returns | use for |
|---|---|---|
| `choice` | `choice`, `probabilities`, `confidence` | who goes next, which category (up to 255 options) |
| `score` | `score` on your scale (fractions allowed), `probabilities`, `confidence` | relevance, readiness, urgency |
| `boolean` (the SDK calls it `noul`) | `probability` of yes, 0 to 1 | approval checks, yes/no gates |

**The question id carries no meaning.** Jev never sees the id, so naming a question
`safe_to_publish` tells it nothing. Put the requirement in `instructions` and describe
every option in `criteria`. Every question is evaluated against the whole state. To ask
about one item, name it: *"Consider ONLY source 's3' in state.sources"*.

**Give it evidence, not status.** "The researcher finished" tells Jev less than the
sources, findings and remaining gaps. Keep the original request in its own field.

## 3. Rebuild the menu every turn

Browser Use builds a fresh list of the controls on the page after every click and lets
Jev pick from that list. Do the same here:
- Offer only the workers that exist and are available now.
- Include current source ids when selecting material.
- Refresh the options after any tool changes the state.

Otherwise Jev is choosing from yesterday's menu. For long candidate lists, filter the
obvious mismatches in code, score what's left, then run a `choice` over the shortlist.

## 4. Ask everything at once

Questions in one request are evaluated in parallel against shared state, and only input
is billed. If the worker, urgency and approval questions can all read the same state, send
them together. Questions cannot see each other's answers, so a decision that needs a
fresh search result has to wait until the search has run. Speculative branching works
too: ask about every possible next action, then use only the answer for the branch you take.

Before paying for a faster model, look for repeated calls. Browser Use cut median browser
calls from 1,092 to 101 with the same models, by reading page state once and not
re-predicting on irrelevant animations.

## 5. Thresholds: gate on the bad outcome, not on confidence

- Confidence is not an accuracy percentage. Set thresholds from labelled examples of your
  own traffic. The article starts review at 0.85 and then tunes it.
- **Gate on the probability of the bad outcome.** A safe action with P(bad)=0 but
  confidence 0.69 fails a 0.90 confidence gate. A gate that fires on safe input trains
  people to click through it, and then gets switched off.
- **Gate only what leaves the machine.** Sending, publishing, paying and deploying get a
  Jev check. Local, reversible work (drafting, saving to a queue, reading) does not. Gating
  everything turns every step into a manual approval, and people stop reading them.
- A boolean near 0.5 means Jev is unsure. Send that to review, not to a coin flip.
- Scores are comparable only within one request. Re-score the finalists together before
  choosing among them.

## 6. Give overnight work somewhere to stop

For a morning briefing: collect sources and write the draft automatically, then **stop at
review**. Publishing requires a separate permission check.

The loop also needs:
- an action limit and a spending limit, enforced in code;
- progress saved after every action;
- after an interruption, a check of the last completed action before repeating anything.

**Check the outcome separately from the decision.** A confident "done" doesn't prove a
file was saved or a message was sent. Browser Use checks the result independently after
Jev selects DONE. Check that the draft file exists, and count an empty worker result as a
failure, not a success.

## 7. Measure the bill per completed task

Jev costs about $0.042 per million input tokens, with no output charge. At about 1,000
input tokens per decision, 10,000 decisions cost about $0.42. That's cheap enough that the
decision's own price rarely matters. The price of a wrong decision does: a cheap call that
sends a worker down the wrong branch costs that worker's whole run plus the retry. Track:
- cost per **completed** task, with failed and empty runs counted against the successes;
- how often each route was taken and how it ended.

## Wiring it to existing workers

Save each decision as a JSON handoff in a local queue (`queue/research`, `queue/write`,
`queue/review`) containing the request, progress, Jev's choice, its probabilities and the
destination. Each worker consumes its own queue.

Then:
- prevent duplicate processing;
- save progress after each action;
- add call and spending limits;
- send uncertain decisions to review;
- keep publishing behind approval;
- list any connectors that are missing rather than guessing.

## Errors

| code | meaning |
|---|---|
| 401 | bad key |
| 422 (direct) / 400 (gateway) | a request field is wrong; the error names it |
| 429 | rate limited; honour `Retry-After` (often 60s) |
| 503 / 529 | overload; back off and retry later |

When Jev is unreachable, treat anything outbound as "ask a human", never as permission.
Local work can continue.
