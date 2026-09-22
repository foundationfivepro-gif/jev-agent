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
