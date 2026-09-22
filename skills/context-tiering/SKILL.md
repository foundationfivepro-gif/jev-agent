---
name: context-tiering
description: Decide per chunk whether to include, index, or exclude it from an agent's context window. Use when building dynamic context selection, retrieval, or compaction for a coding agent — the single largest token saving available.
---

# Include, index, or exclude — not summarize

**Tools** (the `jev` MCP server): `jev_select_context(goal, root, globs, budget_tokens)`
scores a directory and returns the three tiers — call it **before** reading any file.
`jev_file_outline(paths)` gives exported symbols with no model call at all.

Reading and searching are ~56% of agent tool-use turns and ~46% of main-agent tokens, so
filtering what gets read is the largest single lever on cost. Measured on 188 TypeScript
files: **508,264 → 3,944 tokens, 99% eliminated**, for about a tenth of a cent.

| tier | holds | when |
|---|---|---|
| **include** | the chunk verbatim | scores relevant, or it is small |
| **index** | path, doc line, exported symbols | plausibly relevant, not clearly needed |
| **exclude** | nothing | scores irrelevant |

## Why not "summarize"

Tested with the original source visible to the judge:

| check | result |
|---|---|
| could a developer modify this from the summary alone | **0.41 – 0.47, fails** |
| does it point at the symbols they need | **0.77 – 0.94, passes** |

Both a cheap model and a careful hand-written summary failed substitution, so a better
summarizer is not the fix. Summaries point well and replace badly — and pointing is what a
parser does for free, exactly, with no second model. Scoring a summary **blind** is
systematically optimistic (0.73 vs 0.47 on the same summary): show the evaluator the original.

## The size floor

For a 164-token declarations file, a cheap summary was **320%** of the original and a careful
one **132%** — both larger than the file they replaced. When the content *is* the identifiers
there is nothing to compress. **If the representation would exceed ~50% of the original,
include the original**; never index a chunk under ~500 tokens.

## Building it

1. Score against the **current** task — relevance is a function of the query.
2. Score over cheap material: path, signatures, a 200-char head. Never full contents.
3. Build the index tier with a **parser** (`ast`, ast-grep, tree-sitter), not a model.
4. Two thresholds: include above, exclude below, index between.
5. **Pin the request** — the original ask is never scored away.
6. **Canary every run.** Relevance scorers fail silently; include known-relevant and
   known-irrelevant items and assert they separate.

A path-substring ground truth misleads: in one run the "misses" were an algorithm-constants
file and a UTF-8 helper, and the "false positives" were JWK handling and bearer-token
extraction — both genuinely relevant. Build a real labelled set before tuning.
