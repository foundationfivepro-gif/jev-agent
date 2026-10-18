---
name: jev-chief-of-staff
description: Run a Chief-of-Staff agent's small decisions (which worker acts next, is this source relevant, is the draft ready, does this need approval) on TypeSafe's Jev instead of a frontier LLM. Use when building or tuning a dispatcher that hands work between research, writing and review agents, or when an agent spends LLM calls on yes/no, route or score judgements.
metadata:
  version: "1.1.0"
---

# Jev as the Chief of Staff's decision layer

An LLM creates the work, Jev decides what happens next, and code carries the decision out.

| belongs to | examples |
|---|---|
| **the LLM** | research, plan, write, fill a form field |
| **Jev** | advise routing, score, propose review/escalation |
| **code** | exact rules (stop after 10 actions, spend cap), execution, saving files |

## 1. Find the decisions
In a job like *"Research three AI-agent tools and draft tomorrow's briefing for my review"*
the forks are: enough sources? which worker next? draft ready? Each is a Jev question;
fetching, writing and saving are not. Replace the most frequent fork first, measure, repeat.

## 2. Ask through the existing guarded service

Routing knowledge version 1.1.0. Read the canonical `jev-routing` skill first
(`agency-work/rules/skills/jev-routing/SKILL.md`, mirrored in this repo's
`skills/jev-routing/SKILL.md`). It defines valid subscription model IDs, efforts,
Cursor's strict allowlist, host routes and zero-spend verification examples.

Use the exact installed typed contract in `/opt/jev-decide/registry/` via
`/usr/local/bin/jev-decide` on the VPS. Mac uses the verified `forwardspan-vps`
SSH alias with the JSON producer inside the remote command and stdin closed.
Cloud without a provisioned authenticated bridge reports `source=fallback`;
never export credentials into a repository. Do not invent registry types.

The 2026-10-18 evaluation read back configured model `jev-1.14.0`; the service
owns that pin. Legacy direct-provider recipes and floating `jev-latest` are not
the current model-selection interface. No service, hook or MCP implementation
is changed by installing this knowledge. Apply privacy/spend gates locally
before any Jev, MCP, SSH or provider call. Send permitted metadata only: Marsh
skips Jev, Jefferies/employer data is blocked, and sensitive/internal requests
stay held. Jev's advice never supplies action approval. No-spend verification
uses only `--dry-run --selftest`, not live model inference. Simulator output is
uncalibrated test evidence even when it says `auto_apply`.

## 3. Rebuild the menu every turn
Offer only workers available now, current source ids, and options refreshed after any state
change. For long candidate lists, filter mismatches in code, score the rest, then `choice`
over the shortlist.

## 4. Ask everything at once
Use only batching supported by the installed typed contract. A decision that needs
a fresh result waits for it. Do not invent a batch API, assume provider pricing,
or consume credits without the task's existing spending authority.

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
