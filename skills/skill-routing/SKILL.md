---
name: skill-routing
description: Pick which saved skill should handle a request using Jev, or find out why the wrong skill keeps loading. Use when many skills overlap, to try the picker on a request, to switch per-message skill suggestions on or off, or to test and tighten skill descriptions.
---

# Let Jev point at the skill

**Tool**: `jev_route_skill(request, project?, skills?)`. It returns `selected` (a skill name, or
`none`), `confidence`, `source` and a one-line `note`. Jev only points: it never runs a skill
and never writes anything.

- **`selected` is a skill**: load it, and tell the user the `note` in one line, e.g.
  `jev: skill docx (82% sure)`.
- **`selected` is `none`**: pick the skill the normal way. This covers four cases:
  - nothing cleared the bar (0.60)
  - Jev chose "none of these"
  - Jev named a skill that is not on the list
  - Jev failed or took longer than 500ms (`source` `timeout` or `unavailable`)

  None of these is an error, so don't report it.

**Where the list comes from.** The local server reads the skill list fresh from disk on each
call:
- every `SKILL.md` under `~/.claude/skills`
- the project's `.claude/skills` and `skills/`
- enabled plugins, named `plugin:skill`

On claude.ai, mobile or Cowork, the remote connector cannot see your skills. Pass `skills` as
`{name: description}` for every skill you were given.

**Try it on any request**
```
python3 skill_router.py try "make a one-pager from these notes"
python3 skill_router.py list        # the skills Jev chooses from
```
In a session with the jev server, call `jev_route_skill` on the request.

**Every message (off by default).** The `UserPromptSubmit` hook adds the note to the prompt
only when the switch is on:
```
python3 skill_router.py on | off | status     # or JEV_SKILL_ROUTER=1 for one shell
```
The suggestion never blocks a message. Past 500ms, or when Jev is down, the hook adds nothing.

**Large skill lists.** Skills are split into groups of 30, each with its own "none" option,
and all groups are asked in parallel. Group winners that clear the bar then meet in a final
round, which also includes "none". A single winner skips the final round.

**Testing and tightening.** Write the expected skill for each request before running:
```
python3 skill_router.py test cases.json   # [{"request": "...", "expected": "docx" | "none"}]
```
The output shows three things:
- **A table.** Each row's outcome is `hit`, `wrong-above-bar` (the costly one) or `fell back`
  (below the bar, so the normal pick applies).
- **A bar sweep.** It shows what 0.4 to 0.8 would have done with the same answers.
- **Descriptions for misses.** It prints the current description of every skill involved in
  a miss.

The usual fix is a clearer `description:` line in that skill's SKILL.md. Say what the skill
is for, and when to use it rather than its nearest neighbour. That one line also improves
Claude's own skill selection, not just Jev's. Set the bar with
`JEV_SKILL_MIN_CONFIDENCE` and the time limit with `JEV_SKILL_DEADLINE_MS`.
