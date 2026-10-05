---
name: jev-chief-of-staff
description: Run a Chief-of-Staff agent's small decisions (which worker acts next, is this source relevant, is the draft ready, does this need approval) on TypeSafe's Jev instead of a frontier LLM. Use when building or tuning a dispatcher that hands work between research, writing and review agents, or when an agent spends LLM calls on yes/no, route or score judgements.
---

# Jev as the Chief of Staff's decision layer

An LLM creates the work, Jev decides what happens next, and code carries the decision out.

| belongs to | examples |
|---|---|
| **the LLM** | research, plan, write, fill a form field |
| **Jev** | route, score, approve, escalate |
| **code** | exact rules (stop after 10 actions, spend cap), execution, saving files |

## 1. Find the decisions
In a job like *"Research three AI-agent tools and draft tomorrow's briefing for my review"*
the forks are: enough sources? which worker next? draft ready? Each is a Jev question;
fetching, writing and saving are not. Replace the most frequent fork first, measure, repeat.

## 2. Ask it
`POST https://openrouter.ai/api/v1/systemone` with `OPENROUTER_API_KEY`, or
`api.typesafe.ai/v1/systemone` with `TYPESAFE_API_KEY` (same body). Model `jev-latest` on both.
For SDK details load the `typesafe-ai` skill.

```json
{"model": "jev-latest",
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
   "needs_publishing": {"type": "noul",
     "instructions": "Does completing state.request require publishing or sending anything?"}}}
```

`choice` returns `choice`, `probabilities`, `confidence` (up to 255 options); `score` returns
`score`, `probabilities`, `confidence`; `noul` returns the probability of yes in its `noul` field.

- **Question ids carry no meaning** — Jev never sees them. Put the requirement in
  `instructions`, describe every option in `criteria`, and name the item when asking about
  one: *"Consider ONLY source 's3' in state.sources"*.
- **Give evidence, not status.** Sources, findings and gaps beat "the researcher finished".
  Keep the original request in its own field.

## 3. Rebuild the menu every turn
Offer only workers available now, current source ids, and options refreshed after any state
change. For long candidate lists, filter mismatches in code, score the rest, then `choice`
over the shortlist.

## 4. Ask everything at once
Questions in one request run in parallel on shared state and only input is billed, so batch
everything that reads the same state. A decision that needs a fresh result waits for it.
Look for repeated calls before paying for a faster model.

## 5. Thresholds
- Confidence is not accuracy. Set thresholds from labelled examples of your own traffic.
- Gate on the probability of the bad outcome, not on confidence.
- Gate only what leaves the machine (send, publish, pay, deploy); local reversible work is not gated.
- A probability near 0.5 goes to review, not a coin flip.
- Scores compare only within one request; re-score finalists together.

## 6. Give unattended work somewhere to stop
Collect and draft automatically, then **stop at review**; publishing needs its own permission
check. Enforce action and spend limits in code, save progress after every action, and after an
interruption check the last completed action before repeating. Verify outcomes separately: a
confident "done" doesn't prove the file exists, and an empty worker result is a failure.

## 7. Measure cost per completed task
Jev costs about $0.042 per million input tokens and nothing for output, so a decision's price
rarely matters; a wrong decision's does. Track cost per **completed** task (failed and empty
runs count) and how often each route was taken and how it ended.

## Wiring
Save each decision as a JSON handoff in a local queue per worker (`queue/research`, `queue/write`,
`queue/review`) with request, progress, Jev's choice and probabilities. Prevent duplicate
processing and list missing connectors rather than guessing.

## Errors
401 bad key · 422 (direct) / 400 (gateway) wrong field, named in the error · 429 honour
`Retry-After` · 503/529 overload, back off. When Jev is unreachable, treat anything outbound
as "ask a human"; local work continues.
