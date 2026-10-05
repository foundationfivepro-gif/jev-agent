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
  - the picked skill's probability is under the bar (0.60)
  - Jev chose "none of these"
  - Jev named a skill that is not on the list
  - Jev failed or took longer than 800ms (`source` `timeout` or `unavailable`)

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
The suggestion never blocks a message. Past 800ms, or when Jev is down, the hook adds nothing.

**Why probability, not confidence.** Jev's Choice `confidence` depends on how many options
there are: a 60% peak is 0.58 confidence among 23 options but 0.40 among 3. The bar is on
the picked skill's probability, which means the same at any size. The result also carries
`jev_confidence` and `separation` (top probability over the runner-up; near 1x is a toss-up).

**Testing, tuning and large lists:** [references/testing.md](references/testing.md). The usual fix
for a miss is a clearer `description:` saying when to use the skill over its nearest neighbour.
