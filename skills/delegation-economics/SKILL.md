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
5. **Sonnet first**; Opus only for long-horizon, open-ended work where Sonnet falls short.
   Fable only when the owner names it. Never stop work for a route.

## Which model

`jev_route_model(task=...)` returns `selected`; pass it verbatim as the Agent tool's `model`.
In Claude Code a hook does this for every spawn without a `model` and appends a return
contract, so call it yourself only where no hook runs. Catalog, prices and the climb rule:
[references/models.md](references/models.md).

## Where to run: local or cloud

`jev_where(task=...)` (or `python3 placement.py "task"`) answers before a session starts: `where` is `cloud` unless the task needs this Mac (Keychain or 1Password, a logged-in browser or desktop app, computer use, launchd, localhost, local files outside a repo, hardware), plus a Sonnet-first `model`. The owner minimises local sessions, so dispatchers (Grok Bot, routines, Codex) start cloud sessions for everything else. In a hooked session the prompt hook prints this once per session.
