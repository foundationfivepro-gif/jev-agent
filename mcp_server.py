"""
jev-agent as an MCP server — one deployment for Claude Code and OpenAI Codex.

Both clients speak MCP over stdio, so the same server registers in either. The
tools here are the decision layer; they never edit files and never run commands.
They tell the calling agent what to read, what is safe, and where data may go.

The tool that matters for cost is `jev_select_context`. Call it BEFORE reading
files, not after: on a 188-file repository it cut 195,103 tokens to 3,947 for
about a tenth of a cent. Everything else is safety and correctness.

Run:
    AI_GATEWAY_API_KEY=... python mcp_server.py

Register (Claude Code):
    claude mcp add jev -- python3 /abs/path/to/mcp_server.py

Register (Codex) in ~/.codex/config.toml:
    [mcp_servers.jev]
    command = "python3"
    args = ["/abs/path/to/mcp_server.py"]
    env = { AI_GATEWAY_API_KEY = "vck_..." }
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import canary  # noqa: E402
from context_tier import Chunk, select  # noqa: E402
from core import Choice, Noul, Score, TransportError, active_transport, decide  # noqa: E402
from permission_gate import extract_commands, gate, hard_block_reason  # noqa: E402
from security_router import LABEL_NAMES, classify  # noqa: E402
from symbols import extract as extract_symbols  # noqa: E402

mcp = FastMCP(
    "jev",
    instructions=(
        "Decision layer backed by the Jev evaluation model. Call jev_select_context "
        "BEFORE reading a repository's files — it is the difference between loading "
        "200k tokens and loading 4k. Call jev_gate_command before running any shell "
        "command, and jev_classify_data before sending file contents to a third party. "
        "These tools decide; they never read, write or execute anything themselves."
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
            "server's env in your MCP config (Claude Code: `claude mcp add jev "
            "--env AI_GATEWAY_API_KEY=... -- python3 mcp_server.py`; Codex: an "
            "`env = { AI_GATEWAY_API_KEY = \"...\" }` key in the [mcp_servers.jev] "
            "table of ~/.codex/config.toml)."
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
    Choose which files to read for a goal, before reading any of them.

    Call this FIRST when working in an unfamiliar repository. It scores every
    candidate on path and exported symbols — never full contents — then sorts
    them into three tiers: read in full, note the location only, or ignore.

    Measured on 188 files: 195,103 candidate tokens reduced to 3,947 read, for
    roughly a tenth of a cent. Reading the whole repository to find three
    relevant files is the single largest avoidable cost in agent work.

    Do not read anything in `index` — that tier exists so you know a file exists
    and what it exports without spending the tokens to load it.
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
    Decide whether a shell command is safe to run, before running it.

    Returns allow, review or block. **Do not execute anything that comes back
    review or block** — surface it to the human instead.

    A deterministic policy runs first and cannot be overridden: it resolves
    absolute paths, follows wrappers like sudo and env, recurses into `sh -c`,
    and splits pipelines, so `/bin/rm -rf /` and `bash -c 'rm -rf /'` are caught
    as readily as `rm -rf /`. Jev then judges whatever survives, and an allow is
    downgraded when the probability mass on a destructive outcome is non-trivial,
    the blast radius is broad, or the model itself asks for a human.
    """
    if not command.strip():
        raise ValueError("command is empty")

    blocked = hard_block_reason(command)
    if blocked:
        return CommandDecision(
            decision="block", reason=blocked,
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
    Classify how sensitive some material is, and which providers may see it.

    Runs entirely in local deterministic code — no model call, no network — so it
    is safe to use on material you have not decided you can share yet. Detects
    PEM and OpenSSH key armour, provider key prefixes, JWTs, connection strings
    with passwords, credential assignments and high-entropy blobs, and fails
    closed on anything credential-shaped.

    Call this before pasting file contents into a third-party service. A `secret`
    label means stop, not proceed carefully.
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
    Ask Jev arbitrary typed questions — the escape hatch for decisions the other
    tools do not cover.

    Good for: routing among options, rubric scoring, classification, verifying
    that a summary retained specific facts, deciding whether a check is worth
    running. Bad for: anything needing prose, exact arithmetic, or a judgement
    the caller could make deterministically in code.

    Booleans return a probability, not a verdict. Pick the threshold yourself
    based on what being wrong costs.
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
    Exported symbols for files, without reading their contents into context.

    Pure local parsing — `ast` for Python, ast-grep for TypeScript and
    JavaScript — so it costs nothing and calls no model. Use it to decide whether
    a file is worth opening, or to answer "what does this module expose" without
    loading the module.
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
    Decide whether a scheduled automation needs to run this time, before running it.

    For recurring jobs — a nightly ingest, a weekly report pull — most executions
    find nothing worth the pipeline. This costs a fraction of a cent and skips
    the rest. Where `jev_select_context` saves tokens within a task, this avoids
    the task entirely, which matters more at high frequency.

    Fails **open**: when uncertain it returns proceed, because skipping a run
    that mattered loses data silently while running a redundant one only costs
    tokens. `escalate` means the signals look anomalous enough that a person
    should look before automation acts — treat it as a stop, not a slow proceed.
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
    Judge a proposed action against allow/block policy written in plain English.

    Use before acting on anything with external effect — posting, emailing,
    paying, deleting. This generalises a hand-maintained list of permitted
    phrasings: a literal list only fires on wording someone anticipated, while
    the same sentences used as criteria let adjacent cases resolve correctly.

    Fails **closed**. Anything not covered by either list comes back `review`,
    never a silent allow, and an otherwise-allowed action that looks hard to
    undo is also sent to review. Do not act on `block` or `review`.
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


if __name__ == "__main__":
    mcp.run()
