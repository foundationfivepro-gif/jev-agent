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

**Compute it, do not assume it.** With X=0.65, Y=0.12, Z=0.23 and a strong model at $15/$75
against a cheap one at $3/$15, delegation wins by ~28%. Drop strong output to $25/M and inline
wins by 33%. Breakeven near $52/M. Substitute today's prices.

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
`model` parameter. It already applies rule 5: below 0.75 confidence it returns Opus, the
strongest ordinary tier. `human` means do not delegate.

In Claude Code this also runs as a `PreToolUse` hook on the Agent tool (`hooks.py
route-agent`), so a subagent spawned without an explicit `model` gets one whether or not
the caller remembered. An explicit `model` is always respected.

The catalog, USD per million tokens, first-party rates:

| key | model | in | out | fit |
|---|---|---|---|---|
| `haiku` | claude-haiku-4-5 | 1 | 5 | classify, format, search-and-report |
| `sonnet` | claude-sonnet-5 | 2 | 10 | day-to-day coding and research |
| `opus` | claude-opus-5 | 5 | 25 | hard debugging, subtle refactors |
| `fable` | claude-fable-5-1 | 10 | 50 | only when Opus is insufficient |

**Fable is the top of the range, not a cheap tier.** It costs twice Opus. It is
escalation-only: the router returns it when Jev proposes it with confidence, never as the
fallback for an uncertain route and never as a default for routine subtasks.
