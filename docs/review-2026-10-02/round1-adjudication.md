# Round 1 adjudication (Claude)

Accepted and implemented: 1 (no `human` candidate; route always returns a model), 2 (Fable
never offered; `allow_escalation` removed; Fable-only catalog -> selected None, hook/MCP use
the floor), 3 (fallback uses offered ordinary tiers only), 4 (one `available_catalog` on all
three surfaces; a restriction is never widened), 5 (`returned_mtok`; skill numbers redone),
6 (outage/no key/no route -> Sonnet on hook and both MCP tools; no second attempt), 8 (index
wording), 9 partly (background spawns no longer counted as `empty`; response shape keys
recorded). Acceptance-based cost per task is deferred: it needs a task-level join that does
not exist yet.

Rejected: 7. Claude Code applies `updatedInput` from a PreToolUse hook together with a
permission decision; without `allow` the routed model/prompt is dropped or the user is
prompted — both contradict goal 1. Subagents' own tool calls still pass normal permissions.
Challenge this if you have evidence that `updatedInput` is honored with no decision.

New in this round, beyond your findings: Sonnet-first fallback. Below 0.75 confidence the
route is Sonnet unless P(opus) >= 1 - sonnet_out/opus_out (0.5 today) — the break-even where
Sonnet-then-Opus-retry stops being cheaper in expectation. Evidence: 41 recorded routes,
12 where Jev preferred Sonnet (0.51-0.69) but the old rule climbed to Opus; Opus measured at
~9x Sonnet cost per completed subagent.
