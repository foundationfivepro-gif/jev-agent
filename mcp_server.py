"""
jev-agent as an MCP server for Claude Code.

The tools here are the decision layer; they never edit files and never run commands.
They tell the calling agent what to read, what is safe, and where data may go.

The tool that matters for cost is `jev_select_context`. Call it BEFORE reading
files, not after: on a 188-file repository it cut 195,103 tokens to 3,947 for
about a tenth of a cent. Everything else is safety and correctness.

Run:
    AI_GATEWAY_API_KEY=... python mcp_server.py

Register:
    claude mcp add jev -- python3 /abs/path/to/mcp_server.py

Every tool description here is loaded into Claude's context, so each one is
written to be as short as it can be while still saying when to call the tool.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402

import canary  # noqa: E402
from context_tier import Chunk, select  # noqa: E402
from core import Choice, Noul, Score, TransportError, active_transport, decide  # noqa: E402
from model_router import DEFAULT_CATALOG, route_model  # noqa: E402
from permission_gate import extract_commands, gate, triage  # noqa: E402
from security_router import LABEL_NAMES, classify  # noqa: E402
from symbols import extract as extract_symbols  # noqa: E402

mcp = MCPServer(
    "jev",
    instructions=(
        "Call jev_select_context BEFORE reading a repository's files (200k tokens -> 4k). "
        "Call jev_classify_data before sending file contents to a third party. Shell "
        "commands and subagent spawns are gated and routed by hooks; call jev_gate_command "
        "(only for commands that send, publish or deploy) or jev_route_model yourself only "
        "where no hook runs. These tools never act."
    ),
)

MAX_FILE_BYTES = 400_000
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
             ".next", "target", "vendor", ".mypy_cache", ".pytest_cache"}
CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs",
                 ".java", ".rb", ".php", ".cs", ".swift", ".kt", ".scala", ".sh",
                 ".sql", ".md", ".yaml", ".yml", ".toml", ".json"}


def _require_key() -> None:
    if not active_transport():
        raise ValueError(
            "AI_GATEWAY_API_KEY is not set for this server process. Add it to the "
            "server's env: `claude mcp add jev --env AI_GATEWAY_API_KEY=... -- "
            "python3 mcp_server.py`."
        )


def _gather(root: Path, globs: list[str] | None) -> list[Path]:
    patterns = globs or ["**/*"]
    seen: dict[Path, None] = {}
    for pattern in patterns:
        for p in root.glob(pattern):
            if not p.is_file():
                continue
            if SKIP_DIRS & set(p.parts):
                continue
            if globs is None and p.suffix.lower() not in CODE_SUFFIXES:
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            seen[p] = None
    return list(seen)


# --------------------------------------------------------------- output models


class ContextSelection(BaseModel):
    """Which files to read, which to merely note, and which to ignore."""

    include: list[str] = Field(description="Read these in full — they are relevant.")
    index: list[str] = Field(
        description="Do NOT read these. Each entry is path + exported symbols, "
        "enough to locate the file if the included set proves insufficient."
    )
    exclude_count: int = Field(description="Files judged irrelevant and omitted entirely.")
    scores: dict[str, float] = Field(description="Relevance probability per included/indexed path.")
    tokens_before: int = Field(description="Approximate tokens if every candidate were read.")
    tokens_after: int = Field(description="Approximate tokens of the included set.")
    saved_pct: int = Field(description="Percentage of candidate context avoided.")
    canary: str = Field(description="Known-answer check. Investigate any non-ok result.")


class CommandDecision(BaseModel):
    """Whether a shell command may run."""

    decision: Literal["allow", "review", "block"]
    reason: str = Field(description="Why this decision was reached.")
    binaries: list[str] = Field(description="Every binary the command line would execute.")
    source: Literal["policy", "model"] = Field(
        description="'policy' means a deterministic rule decided it and cannot be overridden."
    )
    block_probability: float | None = Field(
        default=None, description="Model-assigned probability the command is destructive."
    )
    impact: float | None = Field(default=None, description="0=none, 3=irreversible.")


class DataClassification(BaseModel):
    """Sensitivity of the material, and where it may be sent."""

    label: Literal["public", "internal", "customer", "secret"]
    allowed_providers: list[str] = Field(
        description="Providers permitted for this label. Empty means escalate to a human."
    )
    reasons: list[str] = Field(description="What drove the classification.")
    safe_to_send_externally: bool


class Evaluation(BaseModel):
    """Raw Jev answers for caller-defined questions."""

    answers: dict[str, Any] = Field(description="Question id -> value.")
    certainty: dict[str, float] = Field(description="Question id -> 0..1 certainty.")
    input_tokens: int


class ScheduledRunDecision(BaseModel):
    """Whether a scheduled automation should execute this time."""

    decision: Literal["proceed", "skip", "escalate"]
    reason: str
    proceed_probability: float = Field(description="Probability there is new material to act on.")
    novelty: float = Field(description="0=nothing new, 3=unusually large.")
    source: Literal["policy", "model"]


class ActionPolicyDecision(BaseModel):
    """Whether a proposed action satisfies allow/block policy."""

    verdict: Literal["allow", "block", "review"]
    reason: str
    matched: str = Field(description="Which policy side matched: allowed, blocked or unlisted.")
    confidence: float


class ModelRouteDecision(BaseModel):
    """Which executor model to hand a task to."""

    selected: str = Field(description=(
        "Pass as the Agent tool's model: haiku, sonnet, opus, fable — or 'human' "
        "when none should attempt it unaided."
    ))
    model_id: str | None = Field(description="API model id for `selected`; null for 'human'.")
    proposed: str | None = Field(description="What Jev proposed before the confidence gate.")
    confidence: float = Field(description="Jev's certainty in `proposed`, 0..1.")
    complexity: float | None = Field(
        description="0=mechanical, 1=standard, 2=multi-step, 3=frontier."
    )
    probabilities: dict[str, float] = Field(description="Probability mass per candidate.")
    source: Literal["policy", "model"]
    cost_per_mtok: dict[str, float] = Field(
        description="{'in': .., 'out': ..} USD per million tokens for `selected`."
    )


# ----------------------------------------------------------------------- tools


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
def jev_select_context(
    goal: Annotated[str, Field(description=(
        "What you are actually trying to do, in one sentence. Be specific — relevance "
        "is judged against this exact wording. 'Find where JWT expiry is validated' "
        "works; 'look at auth' does not."
    ))],
    root: Annotated[str, Field(description="Absolute path to the repository or directory.")],
    globs: Annotated[list[str] | None, Field(default=None, description=(
        "Optional glob patterns relative to root, e.g. ['src/**/*.ts']. Omit to scan "
        "common source extensions."
    ))] = None,
    budget_tokens: Annotated[int, Field(default=40000, ge=1000, le=500000, description=(
        "Token budget for the included set."
    ))] = 40000,
) -> ContextSelection:
    """
    Choose which files to read for a goal, before reading any. Call FIRST in an
    unfamiliar repository: scores paths and exported symbols (never contents) into
    read-in-full, index and ignore (188 files: 195k tokens -> 4k). Never read `index`.
    """
    _require_key()
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise ValueError(f"root is not a directory: {base}")

    paths = _gather(base, globs)
    if not paths:
        raise ValueError(
            f"No candidate files under {base}"
            + (f" matching {globs}" if globs else " with known source extensions")
            + ". Widen `globs` or check the path."
        )

    chunks = [Chunk("goal", goal, kind="request")]
    by_id: dict[str, str] = {}
    for i, p in enumerate(paths):
        cid = f"f{i}"
        rel = str(p.relative_to(base))
        by_id[cid] = rel
        chunks.append(Chunk(cid, p.read_text(encoding="utf-8", errors="replace"), path=rel))

    try:
        packed = select(goal, chunks, budget=budget_tokens)
    except canary.CanaryError as exc:
        raise ValueError(
            f"Relevance scoring failed its known-answer check, so the ranking is not "
            f"trustworthy and has been discarded rather than returned. {exc}"
        ) from exc
    except TransportError as exc:
        raise ValueError(f"Jev unreachable: {exc}") from exc

    return ContextSelection(
        include=[by_id[c.id] for c in packed.included if c.id in by_id],
        index=packed.indexed,
        exclude_count=len(packed.excluded),
        scores={by_id[k]: round(v, 3) for k, v in packed.scores.items() if k in by_id},
        tokens_before=packed.tokens_in,
        tokens_after=packed.tokens_out,
        saved_pct=100 - round(100 * packed.tokens_out / max(packed.tokens_in, 1)),
        canary=packed.canary,
    )


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": True},
)
def jev_gate_command(
    command: Annotated[str, Field(description="The exact shell command line you intend to run.")],
    cwd: Annotated[str, Field(default=".", description="Working directory it would run in.")] = ".",
) -> CommandDecision:
    """
    Whether a command that sends, publishes or deploys is safe: allow, review or
    block. Local commands (builds, tests, commits, installs, workspace deletes)
    need no call: they return allow without reaching Jev. Only rm of root, home
    or a wildcard and a few irreversible constructs block. Review means real
    evidence of risk, so ask once for the batch and quote the reason.
    """
    if not command.strip():
        raise ValueError("command is empty")

    kind, reason = triage(command)
    if kind != "external":
        return CommandDecision(
            decision="block" if kind == "block" else "allow", reason=reason,
            binaries=extract_commands(command), source="policy",
        )

    _require_key()
    try:
        d = gate(command, cwd)
    except TransportError as exc:
        # Fail closed: an unreachable judge is not permission to proceed.
        return CommandDecision(
            decision="review",
            reason=f"Jev unreachable ({exc}); failing closed to human review.",
            binaries=extract_commands(command), source="policy",
        )
    probs = d.get("probabilities") or {}
    return CommandDecision(
        decision=d["final"], reason=d.get("reason", ""),
        binaries=d.get("binaries", []), source=d.get("source", "model"),
        block_probability=probs.get("block"), impact=d.get("impact"),
    )


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def jev_classify_data(
    paths: Annotated[list[str], Field(description="File paths involved, relative or absolute.")],
    content: Annotated[str, Field(default="", description=(
        "Optional file content to scan. It is examined locally and is never sent "
        "to any model or returned in the result."
    ))] = "",
) -> DataClassification:
    """
    Sensitivity of material and which providers may see it. Local, no model call.
    Call before sending file contents to a third party. `secret` means stop.
    """
    label, reasons = classify(paths, content)
    name = LABEL_NAMES[label]
    allowed = {
        "public": ["local", "approved_cloud", "public_cloud"],
        "internal": ["local", "approved_cloud"],
        "customer": ["local", "approved_cloud"],
        "secret": [],
    }[name]
    return DataClassification(
        label=name, allowed_providers=allowed, reasons=reasons,
        safe_to_send_externally=name in ("public", "internal"),
    )


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
def jev_evaluate(
    state: Annotated[dict, Field(description=(
        "The shared context every question is judged against. Keep it small — it is "
        "re-sent per batch."
    ))],
    questions: Annotated[list[dict], Field(description=(
        "Each item is {id, type, instructions?, criteria?}. type is 'boolean' "
        "(returns a probability), 'score' (criteria = ordered list low->high, "
        "returns an interpolated value) or 'choice' (criteria = {option: description}). "
        "IMPORTANT: every question is judged against the WHOLE state, so a question "
        "about one part of it must say so explicitly in its instructions — e.g. "
        "\"Consider ONLY state.files.f3\". Omitting that returns confident, uniform, "
        "wrong answers with no error."
    ))],
) -> Evaluation:
    """
    Arbitrary typed questions to Jev (boolean, choice, score) for decisions no other
    tool covers. Not for prose or exact arithmetic. Booleans return a probability;
    choose the threshold by what being wrong costs.
    """
    _require_key()
    if not questions:
        raise ValueError("questions is empty")

    built: dict[str, Any] = {}
    for q in questions:
        qid, qtype = q.get("id"), q.get("type")
        if not qid or not qtype:
            raise ValueError(f"each question needs 'id' and 'type': got {q!r}")
        instructions, criteria = q.get("instructions"), q.get("criteria")
        try:
            if qtype in ("boolean", "noul"):
                built[qid] = Noul(criteria=criteria) if criteria else Noul(instructions=instructions)
            elif qtype == "score":
                if not isinstance(criteria, list):
                    raise ValueError("score criteria must be an ordered list, low to high")
                built[qid] = Score(criteria=criteria, instructions=instructions)
            elif qtype == "choice":
                if not isinstance(criteria, dict):
                    raise ValueError("choice criteria must be {option: description}")
                built[qid] = Choice(criteria=criteria, instructions=instructions)
            else:
                raise ValueError(f"unknown type {qtype!r}; use boolean, score or choice")
        except Exception as exc:
            raise ValueError(f"question {qid!r}: {exc}") from exc

    try:
        d = decide(state, built)
    except TransportError as exc:
        raise ValueError(f"Jev unreachable: {exc}") from exc

    return Evaluation(
        answers={k: a.value for k, a in d.answers.items()},
        certainty={k: round(a.certainty, 3) for k, a in d.answers.items()},
        input_tokens=d.input_tokens,
    )


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def jev_file_outline(
    paths: Annotated[list[str], Field(description="Absolute or relative file paths.")],
) -> dict[str, str]:
    """
    Exported symbols of files without loading them. Local, no model call. Use to
    decide whether a file is worth opening.
    """
    out: dict[str, str] = {}
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.is_file():
            out[raw] = "[not a file]"
            continue
        try:
            out[raw] = extract_symbols(str(p), p.read_text(encoding="utf-8", errors="replace")).index_entry()
        except Exception as exc:
            out[raw] = f"[unreadable: {type(exc).__name__}]"
    return out


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
def jev_should_run(
    automation: Annotated[str, Field(description="Name of the scheduled automation.")],
    purpose: Annotated[str, Field(description="What a full run of it does, in one sentence.")],
    signals: Annotated[dict, Field(description=(
        "Cheap observations only — counts, timestamps, filenames. NEVER the underlying "
        "data; if gathering these is expensive the gate has already lost. Include a "
        "baseline such as typical_new_files_per_run, or 'is this larger than normal' "
        "is unanswerable."
    ))],
    force_after_skips: Annotated[int | None, Field(default=None, description=(
        "Proceed unconditionally after this many consecutive skips. A gate that can "
        "skip forever eventually skips through something real."
    ))] = None,
) -> ScheduledRunDecision:
    """
    Whether a scheduled automation needs to run this time. Fails open (proceed when
    unsure). `escalate` means anomalous signals: stop and ask a person.
    """
    _require_key()
    from harness import should_run

    try:
        d = should_run(automation, purpose, signals, force_if_stale_runs=force_after_skips)
    except TransportError as exc:
        raise ValueError(f"Jev unreachable: {exc}") from exc
    return ScheduledRunDecision(
        decision=d.action, reason=d.reason,
        proceed_probability=round(d.proceed_probability, 3),
        novelty=round(d.novelty, 3), source=d.source,
    )


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": True},
)
def jev_check_action(
    action: Annotated[str, Field(description="The proposed action, described plainly.")],
    allow: Annotated[list[str], Field(description=(
        "Plain-English sentences describing what is permitted. These are matched "
        "semantically, so near-misses of the exact wording still resolve."
    ))],
    block: Annotated[list[str], Field(default=[], description=(
        "Plain-English sentences describing what is forbidden. Evaluated FIRST and "
        "always wins — an action matching both is blocked."
    ))] = [],
) -> ActionPolicyDecision:
    """
    Judge an action with external effect (post, email, pay, delete) against
    plain-English allow/block policy. Fails closed: unlisted or hard-to-undo goes
    to review. Do not act on block or review.
    """
    _require_key()
    from harness import check_action

    try:
        d = check_action(action, allow, block)
    except TransportError as exc:
        # An unreachable judge is not permission to proceed.
        return ActionPolicyDecision(
            verdict="review", reason=f"Jev unreachable ({exc}); failing closed.",
            matched="", confidence=0.0,
        )
    return ActionPolicyDecision(
        verdict=d.verdict, reason=d.reason, matched=d.matched, confidence=round(d.confidence, 3),
    )


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
def jev_route_model(
    task: Annotated[str, Field(description=(
        "The complete subtask you are about to delegate, in one or two sentences. "
        "Include what must come back — the route depends on it."
    ))],
    catalog: Annotated[dict | None, Field(default=None, description=(
        "Override: {name: {fit, cost_in, cost_out, tier, id?, escalation_only?}}."
    ))] = None,
) -> ModelRouteDecision:
    """
    Cheapest model that should pass a subtask. The Agent hook already does this
    for every spawn without an explicit model; call it only where no hook runs.

    Pass `selected` as the model. Below 75% confidence it returns the strongest
    tier Jev gave 20% weight (mechanical tasks: cheap tier accepted from 50%).
    Fable is escalation-only, never a fallback. `human` means do not delegate.
    """
    _require_key()
    if not task.strip():
        raise ValueError("task is empty")

    cat = catalog or DEFAULT_CATALOG
    try:
        d = route_model(task, catalog=cat, max_cost_in=max_cost_in)
    except TransportError as exc:
        raise ValueError(f"Jev unreachable: {exc}") from exc

    chosen = cat.get(d["selected"], {})
    return ModelRouteDecision(
        selected=d["selected"],
        model_id=chosen.get("id"),
        proposed=None if d.get("proposed") is None else str(d["proposed"]),
        confidence=round(float(d.get("confidence", 0.0)), 3),
        complexity=None if d.get("complexity") is None else round(float(d["complexity"]), 2),
        probabilities={k: round(float(v), 3) for k, v in (d.get("probabilities") or {}).items()},
        source=d.get("source", "model"),
        cost_per_mtok={"in": chosen.get("cost_in", 0.0), "out": chosen.get("cost_out", 0.0)},
    )


if __name__ == "__main__":
    mcp.run()
