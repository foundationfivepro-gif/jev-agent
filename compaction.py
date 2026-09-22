"""
Query-aware compaction: represent the same history differently for each task.

Compaction and context selection answer the same question at different moments —
what belongs in the next prompt — so this module uses the same three tiers as
context_tier: full / index / drop. It deliberately does NOT have a "summary"
tier that paraphrases a chunk into prose.

Why: summaries do not substitute. Measured with the originals visible to the
judge, "could a developer work from this instead of the source" scored 0.41-0.47
while "does it point at what they need" scored 0.77-0.94. A summary that seems
to preserve the facts routinely omits the specific literal, branch or default
the next step needs, and nothing in the output says so.

The index tier here is a deterministic *excerpt with provenance*: head, tail,
elided line count, and the chunk id. That is honest about what is missing and
re-retrievable, which a paraphrase is not. It also needs no second model, which
keeps Jev the only external model in the system.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence

import canary
from core import Choice, decide_batched, write_trace

# Below this a chunk is cheaper to keep whole than to represent.
MIN_INDEX_CHARS = 1200

HEAD_LINES = 12
TAIL_LINES = 6


@dataclass
class HistoryChunk:
    id: str
    text: str
    kind: str = "output"        # 'output' | 'message' | 'file' | 'reasoning' | 'constraint'

    @property
    def pinned(self) -> bool:
        # Constraints and policy are never compacted away by a score. If a rule
        # must hold for the whole session, semantic filtering must not be able
        # to drop it — store it deterministically instead.
        return self.kind == "constraint"


def excerpt(text: str, head: int = HEAD_LINES, tail: int = TAIL_LINES) -> str:
    """
    Deterministic head/tail excerpt that states what it removed.

    Not a summary: no claim is made that the elided middle did not matter, only
    that it is elided and where to get it.
    """
    lines = text.splitlines()
    if len(lines) <= head + tail:
        return text
    cut = len(lines) - head - tail
    return "\n".join(lines[:head] + [f"    … {cut} lines elided …"] + lines[-tail:])


def _question(chunk_id: str) -> Choice:
    """Scoped to one chunk by name — see context_tier._question for why."""
    return Choice(
        instructions=(
            f"Consider ONLY the chunk whose id is '{chunk_id}' in state.chunks, "
            f"ignoring every other chunk. How should it appear in the next context?"
        ),
        criteria={
            "full": "Exact wording, code, values or identifiers from it are needed",
            "index": "Its existence and location matter, but the detail can be fetched later",
            "drop": "It cannot change the answer to the next goal",
        },
    )


def compact(goal: str, chunks: Sequence[HistoryChunk], *, min_confidence: float = 0.70) -> dict:
    """
    Re-represent `chunks` for `goal`.

    Fails safe: a low-confidence answer becomes "full", because keeping too much
    costs tokens while dropping the wrong thing costs a correct answer.
    """
    if not chunks:
        return {"levels": {}, "context": [], "canary": "no chunks"}

    scored = [c for c in chunks if not c.pinned]
    pinned = [c for c in chunks if c.pinned]

    state_chunks = {
        c.id: {"kind": c.kind, "chars": len(c.text), "head": c.text[:240]} for c in scored
    }
    questions = {c.id: _question(c.id) for c in scored}

    for probe in (canary.CANARY_RELEVANT, canary.CANARY_IRRELEVANT):
        state_chunks[probe["id"]] = {"kind": "output", "chars": len(probe["text"]),
                                     "head": probe["text"]}
        questions[probe["id"]] = _question(probe["id"])

    def state_for(ids):
        keep = set(ids) | set(canary.CANARY_IDS)
        return {"goal": goal, "chunks": {k: v for k, v in state_chunks.items() if k in keep}}

    result = decide_batched(state_for, questions)

    # Canary check adapted to a Choice: the obviously-relevant probe must not be
    # dropped and the obviously-irrelevant one must not be kept in full.
    yes = result.value(canary.CANARY_RELEVANT["id"])
    no = result.value(canary.CANARY_IRRELEVANT["id"])
    canary_ok = yes != "drop" and no != "full"
    canary_detail = (
        f"canaries ok (relevant={yes}, irrelevant={no})" if canary_ok
        else f"CANARY FAILURE: relevant chunk -> {yes}, irrelevant chunk -> {no}. "
             "Questions are probably not bound to their chunks; levels are unusable."
    )
    if not canary_ok:
        raise canary.CanaryError(canary_detail)

    levels: dict[str, str] = {}
    packed: list[str] = []

    for c in pinned:
        levels[c.id] = "full"
        packed.append(c.text)

    for c in scored:
        answer = result.answers.get(c.id)
        level = str(answer.value) if answer and answer.certainty >= min_confidence else "full"
        # Small chunks are never indexed: an excerpt of a short output costs
        # about what the output costs and is strictly less useful.
        if level == "index" and len(c.text) < MIN_INDEX_CHARS:
            level = "full"
        levels[c.id] = level
        if level == "full":
            packed.append(c.text)
        elif level == "index":
            packed.append(f"[{c.id}] {excerpt(c.text)}")

    before = sum(len(c.text) for c in chunks)
    after = sum(len(p) for p in packed)
    payload = {
        "levels": levels,
        "context": packed,
        "chars_before": before,
        "chars_after": after,
        "saved_pct": round(100 * (1 - after / max(before, 1))),
        "canary": canary_detail,
    }
    write_trace("compaction", {"goal": goal, "chunk_ids": [c.id for c in chunks]},
                {"levels": levels, "saved_pct": payload["saved_pct"], "canary": canary_detail})
    return payload


if __name__ == "__main__":
    demo = [
        HistoryChunk("policy", "Never log secrets or weaken authorization.", kind="constraint"),
        HistoryChunk("ui_chat", "Earlier discussion about button colors. " * 80, kind="message"),
        HistoryChunk("grep", "\n".join(f"src/auth/session.py:{i}: token check" for i in range(60)), kind="output"),
    ]
    out = compact("Audit how secrets are handled", demo)
    print(json.dumps({k: out[k] for k in ("levels", "saved_pct", "canary")}, indent=2))
