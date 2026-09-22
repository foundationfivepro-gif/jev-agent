"""
Shared runtime for every Jev decision module.

Jev is the ONLY external model in this system. Nothing here calls a generative
model; every other step is deterministic code. That is a design constraint, not
an accident — see context_tier.py for why it also produces a better result.

Transport is the Vercel AI Gateway:

    AI_GATEWAY_API_KEY -> ai-gateway.vercel.sh/v1/evaluate, model typesafe-ai/jev

There is deliberately no second transport. A direct `api.typesafe.ai` path was
written and then removed: with no TypeSafe key to exercise it, it would have
been untested code reached only when something had already gone wrong. If that
path is ever wanted, add it back *with* tests against a live key.

`typesafe_sdk` is still a dependency, but only for its question types. Those are
pydantic models that reject a malformed `criteria` locally — a Score given a
string, a Choice given a list — turning what would be a 400 round-trip into an
immediate ValidationError. That is the single easiest mistake to make with this
API, so the local check earns the dependency. The SDK's client is not used.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

# Question types only — the SDK client is intentionally unused (see module docstring).
from typesafe_sdk import Choice, Noul, RetryPolicy, Score

__all__ = [
    "Choice", "Noul", "Score",          # re-exported so modules import one place
    "decide", "decide_batched", "write_trace",
    "Decision", "TransportError", "active_transport", "MAX_QUESTIONS",
]

# ---------------------------------------------------------------- configuration

GATEWAY_URL = os.getenv("AI_GATEWAY_BASE_URL", "https://ai-gateway.vercel.sh/v1")
GATEWAY_MODEL = os.getenv("JEV_GATEWAY_MODEL", "typesafe-ai/jev")

# Measured against this exact transport, so these are production numbers, not
# estimates: ~150 questions succeed in isolation, 170+ returns a 503 that
# survives retries, and ~100 fails intermittently under sustained load. 50 was
# reliable across every run.
#
# Question count is not the only ceiling — state size matters more. See
# decide_batched: a large state re-sent per batch will 503 well under 50.
MAX_QUESTIONS = int(os.getenv("JEV_MAX_QUESTIONS", "50"))

# The ceiling that actually binds. Question count is what people tune, but the
# gateway refuses on request SIZE: enriching each item's state (adding real
# signatures, say) made batches of 50 start failing that had worked at 50 before,
# with no change in count. decide_batched packs to this budget as well as to
# MAX_QUESTIONS, which makes batching robust to whatever callers put in state.
MAX_PAYLOAD_CHARS = int(os.getenv("JEV_MAX_PAYLOAD_CHARS", "24000"))

# The SDK default caps backoff at 5s. Free-tier rate limits ask for 60s, so a
# policy that never waits that long burns its whole budget on calls that cannot
# yet succeed. respect_retry_after is what actually does the work here.
RETRY = RetryPolicy(
    max_retries=3,
    backoff_initial=0.5,
    backoff_max=90.0,
    respect_retry_after=True,
    timeout=30.0,
)

TRACE_DIR = Path(os.getenv("JEV_TRACE_DIR", "traces"))


class TransportError(RuntimeError):
    """Raised when no transport is configured, or the call fails past retries."""


def active_transport() -> str:
    """'gateway', or '' when AI_GATEWAY_API_KEY is unset."""
    return "gateway" if os.getenv("AI_GATEWAY_API_KEY") else ""


# ---------------------------------------------------------------- result shape


@dataclass(frozen=True)
class Answer:
    """One normalized answer. `value` is what you usually want."""

    kind: str                      # 'noul' | 'choice' | 'score'
    value: Any                     # probability | chosen option | interpolated score
    confidence: float | None       # None for noul — see .certainty
    probabilities: Mapping[Any, float] | None

    @property
    def certainty(self) -> float:
        """
        A comparable 0..1 certainty for any kind.

        Noul answers carry no confidence field on either transport, so certainty
        is distance from a coin flip. Choice and score report it directly.
        """
        if self.kind == "noul":
            return abs(float(self.value) - 0.5) * 2
        return float(self.confidence or 0.0)


@dataclass(frozen=True)
class Decision:
    """The result of one decide() call."""

    answers: Mapping[str, Answer]
    model: str
    transport: str
    input_tokens: int
    output_tokens: int

    def __getitem__(self, qid: str) -> Any:
        return self.answers[qid].value

    def value(self, qid: str, default: Any = None) -> Any:
        a = self.answers.get(qid)
        return default if a is None else a.value

    def certainty(self, qid: str) -> float:
        a = self.answers.get(qid)
        return 0.0 if a is None else a.certainty

    def is_true(self, qid: str, threshold: float = 0.5) -> bool:
        """
        Threshold a noul. There is deliberately no default 'truthy' reading of a
        probability: the threshold belongs to the caller, set by the cost of
        being wrong, not to this module.
        """
        a = self.answers[qid]
        if a.kind != "noul":
            raise TypeError(f"{qid!r} is not a noul question")
        return float(a.value) >= threshold


# ---------------------------------------------------------------- transports


def _questions_to_wire(questions: Mapping[str, Any]) -> dict[str, dict]:
    """Serialize SDK question objects into the gateway's JSON shape."""
    wire: dict[str, dict] = {}
    for qid, q in questions.items():
        kind = getattr(q, "type", None)
        if kind is None:
            raise TypeError(f"question {qid!r} must be a Noul, Choice or Score")
        # The gateway names the yes/no primitive 'boolean'; the SDK calls it
        # 'noul'. Same question, same model — only the discriminator differs.
        body: dict[str, Any] = {"type": "boolean" if kind == "noul" else kind}
        if getattr(q, "instructions", None) is not None:
            body["instructions"] = q.instructions
        criteria = getattr(q, "criteria", None)
        if criteria is not None:
            body["criteria"] = criteria if not hasattr(criteria, "model_dump") else criteria.model_dump()
        wire[qid] = body
    return wire


def _call_gateway(state: Any, questions: Mapping[str, Any]) -> Decision:
    key = os.environ["AI_GATEWAY_API_KEY"]
    payload = json.dumps(
        {"model": GATEWAY_MODEL, "state": state, "questions": _questions_to_wire(questions)}
    ).encode()

    last: Exception | None = None
    for attempt in range(RETRY.max_retries + 1):
        req = urllib.request.Request(
            f"{GATEWAY_URL}/evaluate",
            data=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=RETRY.timeout) as resp:
                body = json.loads(resp.read())
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            retryable = exc.code in (408, 429, 500, 502, 503, 504, 529)
            last = TransportError(f"gateway {exc.code}: {detail}")
            if not retryable or attempt == RETRY.max_retries:
                raise last from exc
            wait = float(exc.headers.get("retry-after") or 0) or min(
                RETRY.backoff_initial * 2**attempt, RETRY.backoff_max
            )
            time.sleep(min(wait, RETRY.backoff_max))
        except urllib.error.URLError as exc:
            last = TransportError(f"gateway unreachable: {exc.reason}")
            if attempt == RETRY.max_retries:
                raise last from exc
            time.sleep(min(RETRY.backoff_initial * 2**attempt, RETRY.backoff_max))
    else:  # pragma: no cover - loop always breaks or raises
        raise last or TransportError("gateway call failed")

    answers: dict[str, Answer] = {}
    for qid, a in (body.get("answers") or {}).items():
        kind = a.get("type")
        if kind == "boolean":
            answers[qid] = Answer("noul", a.get("probability"), None, None)
        elif kind == "choice":
            answers[qid] = Answer("choice", a.get("choice"), a.get("confidence"), a.get("probabilities"))
        elif kind == "score":
            answers[qid] = Answer("score", a.get("score"), a.get("confidence"), a.get("probabilities"))
    usage = body.get("usage") or {}
    return Decision(
        answers=answers,
        model=body.get("model", GATEWAY_MODEL),
        transport="gateway",
        input_tokens=usage.get("inputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
    )


# ---------------------------------------------------------------- public API


def decide(state: Any, questions: Mapping[str, Any]) -> Decision:
    """
    Evaluate `state` against `questions` on whichever transport is configured.

    IMPORTANT — question ids do not bind to state keys. Every question is
    evaluated against the WHOLE state; an id of 'f3' has no implicit link to
    state['files']['f3']. A question scoped to one part of the state must say so
    in its instructions. Getting this wrong returns confident, uniform, wrong
    answers with no error. Use canary.py to detect it.
    """
    if not questions:
        raise ValueError("decide() needs at least one question")
    if len(questions) > MAX_QUESTIONS:
        raise ValueError(
            f"{len(questions)} questions exceeds MAX_QUESTIONS={MAX_QUESTIONS}; "
            "use decide_batched()"
        )

    if not active_transport():
        raise TransportError("AI_GATEWAY_API_KEY is not set.")
    return _call_gateway(state, questions)


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def decide_batched(
    state: Any | Callable[[Sequence[str]], Any],
    questions: Mapping[str, Any],
    *,
    size: int | None = None,
) -> Decision:
    """
    Same contract as decide(), for question sets above the per-call ceiling.

    `state` may be a value or a callable taking the batch's question ids and
    returning the state slice for that batch. **Pass a callable when fanning out
    over many items.** A fixed state is re-sent in full with every batch, so a
    188-item fan-out at size=50 sends the whole corpus four times — which is
    both the dominant token cost and, past a few thousand tokens, a 503. Slicing
    the state so each batch carries only the items its questions ask about turns
    that back into roughly one pass over the data.
    """
    size = size or MAX_QUESTIONS
    ids = list(questions)
    slice_state = state if callable(state) else (lambda _ids: state)

    if len(ids) <= size:
        return decide(slice_state(ids), questions)

    merged: dict[str, Answer] = {}
    model = transport = ""
    tin = tout = 0
    for group in _batches(ids, slice_state, questions, size):
        part = decide(slice_state(group), {qid: questions[qid] for qid in group})
        merged.update(part.answers)
        model, transport = part.model, part.transport
        tin += part.input_tokens
        tout += part.output_tokens
    return Decision(merged, model, transport, tin, tout)


def _batches(
    ids: Sequence[str],
    slice_state: Callable[[Sequence[str]], Any],
    questions: Mapping[str, Any],
    size: int,
) -> Iterator[list[str]]:
    """
    Group ids by payload size as well as count.

    Question count is the ceiling people notice; **payload bytes is the one that
    actually binds**. Enriching the state per item — say, adding real signatures
    to each entry — made 50 questions start failing that had worked, without any
    change to the count. Measuring the serialized request and packing to a byte
    budget makes the batcher robust to whatever the caller puts in state.
    """
    per_item: dict[str, int] = {}
    for qid in ids:
        try:
            per_item[qid] = len(json.dumps(slice_state([qid]), default=str)) + len(
                json.dumps(_questions_to_wire({qid: questions[qid]}), default=str)
            )
        except (TypeError, ValueError):
            per_item[qid] = MAX_PAYLOAD_CHARS  # unmeasurable: give it its own call

    group: list[str] = []
    total = 0
    for qid in ids:
        cost = per_item[qid]
        if group and (len(group) >= size or total + cost > MAX_PAYLOAD_CHARS):
            yield group
            group, total = [], 0
        group.append(qid)
        total += cost
    if group:
        yield group


def write_trace(
    system: str, state: Any, decision: Any, result: Any = None
) -> Path:
    """
    Append an auditable record of one decision.

    The state is hashed rather than stored: traces must be safe to keep and to
    ship, and state routinely contains source code or customer data. The hash
    still proves which input produced which decision.
    """
    payload = {
        "time": datetime.now(timezone.utc).isoformat(),
        "system": system,
        "state_hash": hashlib.sha256(
            json.dumps(state, sort_keys=True, default=str).encode()
        ).hexdigest(),
        "decision": decision,
        "result": result,
    }
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = payload["time"][:19].replace(":", "")
    path = TRACE_DIR / f"{system}-{stamp}-{payload['state_hash'][:8]}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path
