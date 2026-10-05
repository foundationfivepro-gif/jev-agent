# Model catalog and routing detail

USD per million tokens, first-party rates; fits from Anthropic's
[model selection matrix](https://platform.claude.com/docs/en/about-claude/models/choosing-a-model).
Check current prices before relying on these numbers.

| key | model | in | cache read | out | fit |
|---|---|---|---|---|---|
| `haiku` | claude-haiku-4-5 | 1 | 0.10 | 5 | classify, format, high-volume or latency-sensitive sub-agent tasks |
| `sonnet` | claude-sonnet-5-5 | 2 | 0.20 | 10 | well-scoped everyday coding, well-defined agent tasks, data analysis, content |
| `opus` | claude-opus-5-5 | 4 | 0.20 | 20 | long-horizon agentic coding and knowledge work, large refactors, hard debugging, vision, computer use |
| `fable` | claude-fable-5-1 | 10 | — | 50 | never routed; only when the owner names it |

## How `jev_route_model` decides
- Jev at least 0.75 confident: its proposal (Haiku, Sonnet or Opus) is taken.
- Less confident: Sonnet, unless Jev's weight on Opus is at least `1 − sonnet/opus` output
  price (0.5 today). Below that, Sonnet first and an Opus retry is cheaper in expectation.
  It is a heuristic until calibrated against outcomes (`hooks.py report`).
- Mechanical tasks (complexity under 0.5) accept a Haiku proposal from 0.5.
- No key, Jev unreachable, a secret-looking task, or no eligible model: Sonnet. Never the
  parent's model by inheritance, never a stop.

In Claude Code this runs as a `PreToolUse` hook on the Agent tool (`hooks.py route-agent`):
a spawn with no `model` gets one plus a return contract; an explicit `model` is kept.

## Price notes
- Cache reads cost the same on Sonnet and Opus 5.5, so on a subagent that mostly re-reads
  cached context the tiers differ almost only in output. Uncached input and output are where
  Opus costs 2×.
- Effort comes before tier: a Sonnet subagent that fell short at `medium` may pass at `high`
  for less than an Opus run. The Agent tool can't set effort per spawn; use an agent type whose
  definition sets higher effort and pass `model: "sonnet"` on the spawn.
