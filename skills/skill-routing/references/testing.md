# Testing and tightening skill routing

Write the expected skill for each request before running:
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
`JEV_SKILL_MIN_PROBABILITY` and the time limit with `JEV_SKILL_DEADLINE_MS`.

## Large skill lists

One question takes up to 255 options, so the whole list is normally
sent at once. Only when it is too big for one request is it split into groups, each with its
own "none" option, asked in parallel. The top 2 skills of each group (at 10% or more) go to a
final round, which also includes "none". A finalist's score is the geometric mean of its
group and final probabilities, as in TypeSafe's hierarchical-classification cookbook.

