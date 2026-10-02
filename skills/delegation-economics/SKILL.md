---
name: delegation-economics
description: Work out whether delegating to a subagent or routing to a different model actually saves tokens, before spawning one. Use when deciding to fan out subagents, route between models mid-session, or split a task across agents.
---

# When delegation actually saves money

"This subtask is easy, send it to a cheaper model" is wrong often enough to be expensive.
Cost is dominated by **how much context is re-processed**, not by subtask difficulty.

```
inline    = big_out·Y + big_in·Z
delegated = small_in·X + small_out·Y + small_in·Z + big_in·R
                ↑ load context          ↑ the parent reads what comes back (R)
```

R is the term that decides it. With a return contract (the hook appends one), R is the
conclusion — a few hundred tokens. Without one, R is everything the delegate produced and
read (Y + Z), and the saving is gone.

**Compute it, do not assume it.** With X=0.65, Y=0.12, Z=0.23 Mtok at today's prices, an
Opus 5.5 parent delegating to Sonnet 5.5 saves 11% when ~250 tokens come back, but costs
31% *more* when the parent absorbs the full transcript; to Haiku, 55% vs 13%.
`estimate_costs(..., returned_mtok=R)` reruns it at your prices and sizes.

## The rule that survives price changes

**Delegate on compression ratio, not difficulty.**

| shape | delegate? |
|---|---|
| Read 50 files, return 3 sentences | Yes — large, price-insensitive |
| Search a big tree, return the hits | Yes |
| Generate code the parent must read in full | No — no compression, pay twice |
| Parent needs the full detail back | No |
| Short session (small X) | No — loading not amortized |

A subagent that returns only a final message is well designed: that return **is** the
compression. One whose transcript the parent re-reads has thrown the advantage away.

## Before spawning

1. Estimate what comes back. More than a few hundred tokens — ask why.
2. Say in the prompt what to return: the conclusion, not the evidence.
3. Parallelise independent delegates in one message.
4. Don't delegate what is already loaded — warm context is nearly free.
5. **Sonnet first.** Sonnet 5.5 handles everyday coding, multi-file edits, research and
   agentic tool use. An uncertain route goes to Sonnet and climbs to Opus only when Jev's
   weight on Opus pays for skipping Sonnet — see below.
6. **Never stop for a route.** No key, no answer, or an unclear task: delegate on Sonnet
   and keep working. Routing never asks a person anything.

## Which model — `jev_route_model`

Once the compression check says delegate, `jev_route_model(task=...)` picks the cheapest
Claude model that should pass and returns `selected` — always a model; pass it verbatim as
the Agent tool's `model` parameter.

- Jev at least 0.75 confident: its proposal (Haiku, Sonnet or Opus) is taken.
- Less confident: Sonnet, unless Jev's weight on Opus is at least `1 − sonnet/opus` output
  price — 0.5 today. Reading that weight as the chance Sonnet falls short, Sonnet first
  and an Opus retry is cheaper in expectation below it. It is a heuristic until calibrated
  against outcomes (`hooks.py report`): it ignores token-volume differences and latency. A 55/44 sonnet/opus split stays on Sonnet; 50/49 for Opus
  goes to Opus.
- Mechanical tasks (complexity under 0.5) accept a Haiku proposal from 0.5: a Haiku retry on
  a one-line edit is nearly free. Otherwise an uncertain route never goes below Sonnet.
- No key, Jev unreachable, a task that looks like it holds a secret, or no eligible model:
  Sonnet. Never the parent's model by inheritance (it may be Opus or Fable), never a stop.

In Claude Code this runs as a `PreToolUse` hook on the Agent tool (`hooks.py route-agent`), so
a subagent spawned without an explicit `model` gets one whether or not the caller remembered.
An explicit `model` is always kept — set `opus` yourself when you know the task needs it.

The catalog, USD per million tokens, first-party rates, fits from Anthropic's
[model selection matrix](https://platform.claude.com/docs/en/about-claude/models/choosing-a-model):

| key | model | in | out | fit |
|---|---|---|---|---|
| `haiku` | claude-haiku-4-5 | 1 | 5 | classify, format, high-volume or latency-sensitive sub-agent tasks |
| `sonnet` | claude-sonnet-5-5 | 2 | 10 | everyday coding, data analysis, content, agentic tool use |
| `opus` | claude-opus-5-5 | 4 | 20 | large refactors, complex systems engineering, hard debugging, vision, computer use |
| `fable` | claude-fable-5-1 | 10 | 50 | never routed; only when the owner names it |

Within one model, effort is often a better lever than switching tiers: Sonnet at higher
effort before Opus.

**Fable is never routed.** It costs 2.5× Opus and is not offered to Jev at all. Use it only
by setting the model yourself when the owner asks for it by name.

In Claude Code the Agent hook does this on every spawn that sets no `model`, and appends a
return contract to the prompt, so the parent reads a conclusion instead of a transcript.
Call `jev_route_model` yourself only where no hook runs.
