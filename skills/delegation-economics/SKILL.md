---
name: delegation-economics
description: Work out whether delegating to a subagent or routing to a different model actually saves tokens, before spawning one. Use when deciding to fan out subagents, route between models mid-session, or split a task across agents.
---

# When delegation actually saves money

"This subtask is easy, send it to a cheaper model" is wrong often enough to be expensive.
Cost is dominated by **how much context is re-processed**, not by subtask difficulty.

```
inline    = big_out·Y + big_in·Z
delegated = small_in·X + small_out·Y + small_in·Z + big_in·(Y + Z)
                ↑ load context          ↑ the parent pays to absorb the result
```

The forgotten term is the last: a delegate's output does not land in the parent's cache free.

**Compute it, do not assume it.** With X=0.65, Y=0.12, Z=0.23 (Mtok) at today's prices:
Opus 5.5 delegating to Sonnet 5.5 costs 31% *more* than doing it inline; to Haiku it saves 13%;
Fable delegating to Sonnet saves 22%. The narrower the price gap, the less delegation pays.
Rerun `estimate_costs` when prices change.

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
5. **Fail toward capability.** An uncertain route goes to the stronger model; a cheap failure
   costs the cheap attempt, the expensive retry, and the latency of noticing.

## Which model — `jev_route_model`

Once the compression check says delegate, `jev_route_model(task=...)` picks the cheapest
Claude model that should pass and returns `selected` — pass it verbatim as the Agent tool's
`model` parameter. It already applies rule 5: below 0.75 confidence it returns the strongest
ordinary tier it gave at least 20% weight to, and Opus only from 40% — so a sonnet/haiku split stays
on Sonnet, a 68/32 sonnet/opus split stays on Sonnet, while 40%+ on Opus or any 20%+ weight on Fable or
`human` means Opus — except on a mechanical task (complexity under 0.5), where a cheap
tier is accepted from 0.5, because a Haiku retry on a one-line edit is nearly free. `human`
means do not delegate.

In Claude Code this also runs as a `PreToolUse` hook on the Agent tool (`hooks.py
route-agent`), so a subagent spawned without an explicit `model` gets one whether or not
the caller remembered. An explicit `model` is always respected.

The catalog, USD per million tokens, first-party rates, fits from Anthropic's
[model selection matrix](https://platform.claude.com/docs/en/about-claude/models/choosing-a-model):

| key | model | in | out | fit |
|---|---|---|---|---|
| `haiku` | claude-haiku-4-5 | 1 | 5 | classify, format, high-volume or latency-sensitive sub-agent tasks |
| `sonnet` | claude-sonnet-5-5 | 2 | 10 | everyday coding, data analysis, content, agentic tool use |
| `opus` | claude-opus-5-5 | 4 | 20 | large refactors, complex systems engineering, hard debugging, vision, computer use |
| `fable` | claude-fable-5-1 | 10 | 50 | work Opus falls short on: hours-long agent sessions, deep research, finished documents and decks |

Anthropic's own default is Opus 5.5, moving to Fable only when Opus at `xhigh`/`max` effort
still falls short — the same shape as the router's fallback. Within one model, effort is often
a better lever than switching tiers.

**Fable is the top of the range, not a cheap tier.** It costs 2.5× Opus. It is
explicit-only: the router never returns it, even on a confident proposal (that means Opus).
Use it only by setting the model yourself for a task that specifically calls for it.

In Claude Code the Agent hook does this on every spawn that sets no `model`, and appends a
return contract to the prompt, so the parent reads a conclusion instead of a transcript.
Call `jev_route_model` yourself only where no hook runs.
