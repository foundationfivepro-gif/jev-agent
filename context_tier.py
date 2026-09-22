"""
Dynamic context: decide per chunk whether to include, index or exclude.

The obvious three-way choice is include / summarize / drop. The summarize tier
does not survive testing on code, so this module replaces it with an INDEX tier
built by a parser instead of a model:

    include   the chunk verbatim
    index     path, doc line and exported symbols — enough to FIND it again
    exclude   nothing

Measured, with the original source visible to the judge:

    "could a developer modify this from the summary alone"   0.41 - 0.47   fails
    "does this point at the symbols they need"               0.77 - 0.94   passes

Both a cheap model and a careful hand-written summary failed substitution, so a
better summarizer is not the fix. And for small files the summary came out at
132-320% of the original, which is not compression at all — hence MIN_INDEX_TOKENS.

Jev scores relevance. Everything else is deterministic. No generative model is
called anywhere in this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import canary
from core import Noul, Decision, decide_batched, write_trace
from symbols import extract

# Below this, indexing costs more than including — never index a small chunk.
MIN_INDEX_TOKENS = 500

INCLUDE_AT = 0.70   # score at or above this: include verbatim
INDEX_AT = 0.25     # between this and INCLUDE_AT: index entry only; below: drop

DEFAULT_BUDGET = 60_000


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class Chunk:
    id: str
    text: str
    path: str = ""
    kind: str = "file"          # 'file' | 'request' | 'output' | 'message'
    tokens: int = 0

    def __post_init__(self) -> None:
        self.tokens = self.tokens or _tokens(self.text)

    @property
    def pinned(self) -> bool:
        """The original request is never scored away."""
        return self.kind == "request"


@dataclass
class Packed:
    included: list[Chunk] = field(default_factory=list)
    indexed: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    canary: str = ""

    def render(self) -> str:
        parts = [c.text for c in self.included]
        if self.indexed:
            parts.append(
                "# Not included — open these if you need them\n" + "\n".join(self.indexed)
            )
        return "\n\n".join(parts)

    def summary(self) -> str:
        return (
            f"{len(self.included)} included, {len(self.indexed)} indexed, "
            f"{len(self.excluded)} excluded | {self.tokens_out}/{self.tokens_in} tokens "
            f"({100 - round(100 * self.tokens_out / max(self.tokens_in, 1))}% saved)"
        )


def _question(chunk_id: str) -> Noul:
    """
    Scoped to one chunk, by name.

    This f-string is load-bearing, not cosmetic. Every question is evaluated
    against the whole state; without naming the chunk, each question scores the
    corpus as a whole and every answer comes back nearly identical. Measured
    separation drops from 1.91 to 0.01 — and nothing raises. See canary.py.
    """
    return Noul(
        instructions=(
            f"Consider ONLY the chunk whose id is '{chunk_id}' in state.chunks, "
            f"ignoring every other chunk. Could that chunk materially change the "
            f"answer to the stated goal?"
        )
    )


def select(
    goal: str,
    chunks: Sequence[Chunk],
    *,
    budget: int = DEFAULT_BUDGET,
    include_at: float = INCLUDE_AT,
    index_at: float = INDEX_AT,
    strict_canary: bool = True,
    refine_top: int = 25,
) -> Packed:
    """
    Score every chunk against `goal` and pack the context.

    Canary chunks with known answers are added to every run and stripped from
    the result. If they fail to separate, the scores are meaningless and this
    raises rather than returning a plausible-looking ranking.

    `refine_top` re-scores that many leading candidates in a single call so the
    boundary decisions are made on comparable numbers; set it to 0 to skip.
    """
    if not chunks:
        return Packed()

    scored_chunks = [c for c in chunks if not c.pinned]
    pinned = [c for c in chunks if c.pinned]

    # Score over cheap material: path, symbols and a short head — never whole
    # file bodies. Feeding the corpus in to decide what to read defeats the point.
    state_chunks: dict[str, dict] = {}
    questions: dict[str, Noul] = {}
    for c in scored_chunks:
        sym = extract(c.path, c.text) if c.path else None
        state_chunks[c.id] = {
            "path": c.path or c.id,
            "kind": c.kind,
            "symbols": sym.exports[:10] if sym else [],
            "head": c.text[:200],
        }
        questions[c.id] = _question(c.id)

    for probe in (canary.CANARY_RELEVANT, canary.CANARY_IRRELEVANT):
        state_chunks[probe["id"]] = {"path": probe["id"], "kind": "file",
                                     "symbols": [], "head": probe["text"]}
        questions[probe["id"]] = _question(probe["id"])

    # Slice the state per batch: each call carries only the chunks its own
    # questions ask about, plus the canaries. Sending the whole corpus with
    # every batch is the dominant cost and, past a few thousand tokens, a 503.
    def state_for(ids):
        keep = set(ids) | set(canary.CANARY_IDS)
        return {"goal": goal, "chunks": {k: v for k, v in state_chunks.items() if k in keep}}

    result: Decision = decide_batched(state_for, questions)

    probe_result = canary.check_separation(
        result, [canary.CANARY_RELEVANT["id"]], [canary.CANARY_IRRELEVANT["id"]]
    )
    if strict_canary:
        probe_result.raise_if_failed()

    scores = {c.id: float(result.value(c.id, 0.0)) for c in scored_chunks}

    # Second pass: re-score the shortlist together.
    #
    # Scores are only comparable WITHIN a batch. Each question is told to judge
    # one chunk, but the batch's other chunks are still in state and shift the
    # result — measured directly: the same corpus split into smaller batches
    # moved recall from 7/8 to 6/8 with no other change. Ranking across batches
    # therefore compares numbers produced under different conditions.
    #
    # The fix is cheap because it only matters at the boundary: take the top
    # candidates and score them once, together, so the decisions that actually
    # change the packed context are made on a single common footing.
    if refine_top and len(scored_chunks) > refine_top:
        shortlist = sorted(scored_chunks, key=lambda c: scores[c.id], reverse=True)[:refine_top]
        short_ids = [c.id for c in shortlist]
        refine_state = {
            "goal": goal,
            "chunks": {cid: state_chunks[cid] for cid in short_ids + list(canary.CANARY_IDS)},
        }
        refine_questions = {cid: questions[cid] for cid in short_ids + list(canary.CANARY_IDS)}
        try:
            refined = decide_batched(lambda _ids: refine_state, refine_questions)
            canary.check_separation(
                refined, [canary.CANARY_RELEVANT["id"]], [canary.CANARY_IRRELEVANT["id"]]
            ).raise_if_failed()
            for cid in short_ids:
                scores[cid] = float(refined.value(cid, scores[cid]))
        except (canary.CanaryError, Exception) as exc:  # noqa: B014 - keep pass one
            # A failed refinement must not discard a usable first pass.
            probe_result = probe_result.__class__(
                probe_result.ok, probe_result.separation, probe_result.positive_mean,
                probe_result.negative_mean,
                probe_result.detail + f" | refine pass skipped: {type(exc).__name__}",
            )

    packed = Packed(scores=scores, canary=probe_result.detail)
    packed.tokens_in = sum(c.tokens for c in chunks)

    for c in pinned:                       # the request always goes in first
        packed.included.append(c)
    used = sum(c.tokens for c in pinned)

    for c in sorted(scored_chunks, key=lambda x: scores[x.id], reverse=True):
        score = scores[c.id]
        entry = extract(c.path, c.text).index_entry() if c.path else f"{c.id} (no path)"

        if score < index_at:
            packed.excluded.append(c.id)
            continue

        # Small chunks are never indexed: a pointer to a 200-token file costs
        # nearly what the file costs, and the file is strictly more useful.
        wants_full = score >= include_at or c.tokens < MIN_INDEX_TOKENS
        if wants_full and used + c.tokens <= budget:
            packed.included.append(c)
            used += c.tokens
        else:
            packed.indexed.append(entry)
            used += _tokens(entry)

    packed.tokens_out = used
    write_trace(
        "context_tier",
        {"goal": goal, "chunk_ids": [c.id for c in chunks]},
        {
            "included": [c.id for c in packed.included],
            "indexed": len(packed.indexed),
            "excluded": packed.excluded,
            "scores": scores,
            "canary": probe_result.detail,
        },
    )
    return packed


if __name__ == "__main__":
    demo = [
        Chunk("goal", "Fix the login redirect loop", kind="request"),
        Chunk("auth", "def login(user):\n    return redirect('/home')\n" * 40, path="src/auth/session.py"),
        Chunk("css", "body { color: red; }\n" * 60, path="web/styles/theme.css"),
    ]
    out = select("Fix the login redirect loop", demo)
    print(out.summary())
    print(json.dumps({"scores": out.scores, "canary": out.canary}, indent=2))
