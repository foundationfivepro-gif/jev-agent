"""
Remote MCP server — the subset of jev-agent that works without your filesystem.

Claude custom connectors reach a public HTTPS endpoint, which makes these tools
available on mobile and claude.ai as well as desktop. Add it at
Customize -> Connectors -> Add custom connector, pointing at `https://<host>/mcp`.

WHAT IS AND IS NOT HERE, AND WHY

Seven tools are stateless — they judge what you pass them — so they work fine from
a server that has never seen your disk:

    jev_evaluate        arbitrary typed decisions
    jev_should_run      should a scheduled automation execute this time
    jev_check_action    does a proposed action satisfy policy
    jev_gate_command    is this shell command safe to run
    jev_route_model     cheapest Claude model that should pass a task
    jev_route_skill     which one skill should handle a request (you pass the list)
    jev_classify_paths  sensitivity from FILE PATHS ONLY

Two tools are deliberately absent:

    jev_select_context   reads your repository. The 98% context saving comes from
                         looking at your actual files; no public server can do
                         that, and shipping a version that uploads your codebase
                         to get the same answer would defeat the purpose.
    jev_file_outline     same reason.

And one is deliberately reduced. The local `jev_classify_data` scans file
CONTENT for credentials and never transmits it. A remote version would require
uploading the very material it exists to protect — a secret scanner you feed
secrets to is worse than none, because it is trusted. `jev_classify_paths` takes
paths only: weaker, and honest about it. Keep the local server for content.

AUTHENTICATION

The tools spend your Jev quota, so an unauthenticated public URL is a bill
someone else can run up. Set JEV_REMOTE_TOKEN and the server rejects anything
without it. Two ways to present it:

    header (preferred)  Add custom connector -> No sign-in -> Request headers
                        authorization = "Bearer <token>"
    path (fallback)     Deploy at /mcp and use URL https://host/t/<token>/mcp
                        if your org lacks the request-headers beta

The path form puts a credential in a URL, which ends up in logs and history.
Prefer the header. If you use the path form, treat the token as disposable and
rotate it freely — it grants only these seven tools.

Run locally:   JEV_REMOTE_TOKEN=x OPENROUTER_API_KEY=... python remote_server.py
Deploy:        see vercel.json and api/index.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Serverless platforms mount the code read-only; /tmp is the writable scratch
# space. Traces there are per-instance and short-lived, which is fine — they
# are an audit aid, not the product. Must be set before core is imported.
os.environ.setdefault("JEV_TRACE_DIR", "/tmp/jev-traces")

from mcp.server.mcpserver import MCPServer  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from core import (  # noqa: E402
    Choice, Noul, Score, TransportError, active_transport, decide, unreachable,
)
from model_router import (  # noqa: E402
    DEFAULT_CATALOG, available_catalog, eligible_models, floor_model, route_model,
)
from permission_gate import extract_commands, gate, triage  # noqa: E402
from security_router import LABEL_NAMES, classify  # noqa: E402

TOKEN = os.getenv("JEV_REMOTE_TOKEN", "")

mcp = MCPServer(
    "jev-remote",
    instructions=(
        "Jev decision tools that need no access to your filesystem. Use "
        "jev_check_action before anything with external effect, jev_should_run "
        "before a scheduled automation executes, and jev_gate_command before a "
        "shell command that sends, publishes or deploys (local commands need no "
        "call). For repository context selection, use the LOCAL jev "
        "server — that capability cannot work remotely."
    ),
)


def _require_key() -> None:
    if not active_transport():
        raise ValueError(
            "The server is missing OPENROUTER_API_KEY (or TYPESAFE_API_KEY). This is a server-side "
            "configuration problem, not something you can fix from the client."
        )


# ------------------------------------------------------------- output models


class Evaluation(BaseModel):
    answers: dict[str, Any] = Field(description="Question id -> value.")
    certainty: dict[str, float] = Field(description="Question id -> 0..1 certainty.")
    input_tokens: int


class ScheduledRunDecision(BaseModel):
    decision: Literal["proceed", "skip", "escalate"]
    reason: str
    proceed_probability: float | None  # None when Jev did not answer
    novelty: float | None
    source: Literal["policy", "model"]


class ActionPolicyDecision(BaseModel):
    verdict: Literal["allow", "block", "review"]
    reason: str
    matched: str
    confidence: float


class CommandDecision(BaseModel):
    decision: Literal["allow", "review", "block"]
    reason: str
    binaries: list[str]
    source: Literal["policy", "model", "unavailable"]


def _route_or_floor(task: str, cat: dict, **kw) -> dict:
    """Route with Jev; with no key, no answer or no route, the floor tier."""
    eligible = eligible_models(cat, **kw)
    d = None
    if active_transport():
        try:
            d = route_model(task, catalog=cat, **kw)
        except Exception:  # outage or anything else: the floor, never a stop
            d = None
    if d is None or d.get("selected") not in eligible:
        floor = floor_model(eligible)
        if floor is None:
            raise ValueError("No model is eligible to route to (JEV_MODELS, the catalog or "
                             "max_cost_in excludes all). Do the work inline; do not stop.")
        d = {"selected": floor, "proposed": None, "confidence": 0.0, "complexity": None,
             "probabilities": {}, "source": "fallback"}
    return d


class ModelRouteDecision(BaseModel):
    selected: str = Field(description="haiku, sonnet or opus. Always a model: routing never stops work.")
    model_id: str | None
    proposed: str | None
    confidence: float
    complexity: float | None = Field(description="0=mechanical, 1=standard, 2=multi-step, 3=frontier.")
    probabilities: dict[str, float]
    source: Literal["policy", "model", "fallback"]
    cost_per_mtok: dict[str, float]


class PathClassification(BaseModel):
    label: Literal["public", "internal", "customer", "secret"]
    reasons: list[str]
    caveat: str = Field(
        default="Paths only — file CONTENT was not examined. A file with an "
        "innocuous name can still hold credentials. Use the local jev server's "
        "jev_classify_data for content scanning; it never transmits what it reads."
    )


class SkillRouteDecision(BaseModel):
    """Which one skill should handle a request, or 'none'."""

    selected: str = Field(description=(
        "Load this skill. 'none' means pick the normal way: nothing cleared the bar, "
        "Jev was unavailable or slow, or no listed skill fits."))
    proposed: str | None = Field(description="What Jev proposed before the confidence bar.")
    confidence: float = Field(description="Jev's certainty in `proposed`, 0..1.")
    reason: str
    source: Literal["policy", "model", "timeout", "unavailable"]
    note: str | None = Field(description="The one line to tell the user; null when 'none'.")
    latency_ms: int


# -------------------------------------------------------------------- tools


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
def jev_evaluate(
    state: Annotated[dict, Field(description="Shared context every question is judged against.")],
    questions: Annotated[list[dict], Field(description=(
        "Each item is {id, type, instructions?, criteria?}. type is 'boolean' "
        "(returns a probability), 'score' (criteria = ordered list low->high) or "
        "'choice' (criteria = {option: description}). Every question is judged "
        "against the WHOLE state, so a question about one part of it must say so "
        "explicitly — otherwise every answer comes back near-identical, with no error."
    ))],
) -> Evaluation:
    """
    Ask Jev arbitrary typed questions.

    Good for routing among options, rubric scoring, classification, and checking
    whether a summary kept specific facts. Bad for prose, exact arithmetic, or
    anything the caller could decide deterministically in code.

    Booleans return a probability, not a verdict — pick the threshold yourself
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
        raise unreachable(exc, "apply the default your caller defined for no answer. "
                           "Silence is neither yes nor a reason to stop.") from exc
    return Evaluation(
        answers={k: a.value for k, a in d.answers.items()},
        certainty={k: round(a.certainty, 3) for k, a in d.answers.items()},
        input_tokens=d.input_tokens,
    )


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
def jev_should_run(
    automation: Annotated[str, Field(description="Name of the scheduled automation.")],
    purpose: Annotated[str, Field(description="What a full run does, in one sentence.")],
    signals: Annotated[dict, Field(description=(
        "Cheap observations only — counts, timestamps, filenames. Include a baseline "
        "such as typical_new_files_per_run, or 'is this larger than normal' cannot "
        "be answered."
    ))],
    force_after_skips: Annotated[int | None, Field(default=None, description=(
        "Proceed unconditionally after this many consecutive skips."
    ))] = None,
) -> ScheduledRunDecision:
    """
    Decide whether a scheduled automation needs to run this time.

    For recurring jobs, most executions find nothing worth the pipeline. Fails
    **open** — when uncertain it proceeds, because skipping a run that mattered
    loses data silently while a redundant run only costs tokens. `escalate` means
    stop and get a person, not proceed carefully.
    """
    _require_key()
    from harness import should_run

    try:
        d = should_run(automation, purpose, signals, force_if_stale_runs=force_after_skips)
    except TransportError as exc:
        # Fails open, as documented: skipping a run that mattered loses data
        # silently; a redundant run only costs tokens.
        return ScheduledRunDecision(
            decision="proceed", reason=f"Jev unreachable ({exc}); failing open.",
            proceed_probability=None, novelty=None, source="policy",
        )
    return ScheduledRunDecision(
        decision=d.action, reason=d.reason,
        proceed_probability=round(d.proceed_probability, 3),
        novelty=round(d.novelty, 3), source=d.source,
    )


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
def jev_check_action(
    action: Annotated[str, Field(description="The proposed action, described plainly.")],
    allow: Annotated[list[str], Field(description="Plain-English sentences describing what is permitted.")],
    block: Annotated[list[str], Field(default=[], description=(
        "Plain-English sentences describing what is forbidden. Evaluated FIRST and always wins."
    ))] = [],
) -> ActionPolicyDecision:
    """
    Judge a proposed action against allow/block policy written in plain English.

    Use before anything with external effect — posting, emailing, paying,
    deleting. Fails **closed**: anything not covered by either list comes back
    `review`, never a silent allow. Do not act on `block` or `review`.
    """
    _require_key()
    from harness import check_action

    try:
        d = check_action(action, allow, block)
    except TransportError as exc:
        return ActionPolicyDecision(verdict="review", reason=f"Jev unreachable ({exc}); failing closed.",
                                    matched="", confidence=0.0)
    return ActionPolicyDecision(verdict=d.verdict, reason=d.reason, matched=d.matched,
                                confidence=round(d.confidence, 3))


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
def jev_gate_command(
    command: Annotated[str, Field(description="The exact shell command line.")],
    cwd: Annotated[str, Field(default=".", description="Working directory it would run in.")] = ".",
) -> CommandDecision:
    """
    Decide whether a shell command is safe to run.

    Only commands that send, publish or deploy need this; local ones return allow
    without a Jev call. Never execute a block whose source is 'policy'. A model
    review or block is advice, never a veto: the user's permission mode decides.
    The deterministic policy runs first and cannot be overridden: it resolves absolute paths, follows
    wrappers like sudo, recurses into `sh -c` and splits pipelines, so
    `bash -c 'rm -rf /'` is caught as readily as `rm -rf /`.
    """
    if not command.strip():
        raise ValueError("command is empty")
    kind, reason = triage(command)
    if kind != "external":
        return CommandDecision(decision="block" if kind == "block" else "allow", reason=reason,
                               binaries=extract_commands(command), source="policy")
    _require_key()
    try:
        d = gate(command, cwd)
    except TransportError as exc:
        return CommandDecision(decision="review", reason=f"Jev unreachable ({exc}); the user's permission mode decides.",
                               binaries=extract_commands(command), source="unavailable")
    return CommandDecision(decision=d["final"], reason=d.get("reason", ""),
                           binaries=d.get("binaries", []), source=d.get("source", "model"))


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
def jev_route_model(
    task: Annotated[str, Field(description=(
        "The complete subtask to delegate, in one or two sentences, including what must come back."
    ))],
    max_cost_in: Annotated[float, Field(default=1e9, description=(
        "Exclude models whose input price per million tokens exceeds this."
    ))] = 1e9,
) -> ModelRouteDecision:
    """
    Pick the cheapest model that should pass a task.

    USD per million tokens: haiku 1/5, sonnet 2/10, opus 4/20, fable 10/50.
    Sonnet-first: below 75% confidence it returns Sonnet unless Jev's weight on
    Opus pays for skipping Sonnet (50% at today's prices); mechanical tasks
    (complexity under 0.5) accept a cheap tier from 50%. Fable is never
    selected; set it explicitly to use it. Jev unavailable means Sonnet, never a stop.
    """
    if not task.strip():
        raise ValueError("task is empty")
    cat = available_catalog()
    d = _route_or_floor(task, cat, max_cost_in=max_cost_in)
    chosen = cat.get(d["selected"], {})
    return ModelRouteDecision(
        selected=d["selected"], model_id=chosen.get("id"),
        proposed=None if d.get("proposed") is None else str(d["proposed"]),
        confidence=round(float(d.get("confidence", 0.0)), 3),
        complexity=None if d.get("complexity") is None else round(float(d["complexity"]), 2),
        probabilities={k: round(float(v), 3) for k, v in (d.get("probabilities") or {}).items()},
        source=d.get("source", "model"),
        cost_per_mtok={"in": chosen.get("cost_in", 0.0), "out": chosen.get("cost_out", 0.0)},
    )


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                       "openWorldHint": False})
def jev_classify_paths(
    paths: Annotated[list[str], Field(description="File paths. Paths only — do NOT paste file contents.")],
) -> PathClassification:
    """
    Classify data sensitivity from file paths alone.

    Deliberately weaker than the local `jev_classify_data`, which scans file
    content for credentials without transmitting any of it. There is no safe
    remote equivalent: a content scanner reached over the internet requires
    uploading the very material it exists to protect.

    So this reads paths only — enough to flag `.env`, `id_rsa`, `*.pem` and
    customer-data directories, and not enough to catch a credential in a file
    with an ordinary name. Runs entirely in local deterministic code on the
    server; no model call.
    """
    label, reasons = classify(paths, "")
    return PathClassification(label=LABEL_NAMES[label], reasons=reasons)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                       "openWorldHint": True})
def jev_route_skill(
    request: Annotated[str, Field(description="The user's request, as they wrote it.")],
    skills: Annotated[dict[str, str], Field(description=(
        "{name: one-line description} of every skill available to you. This server "
        "cannot see your skills, so pass the names and descriptions you were given."
    ))],
) -> SkillRouteDecision:
    """
    Which one of your skills should handle a request, or 'none'. Only points:
    never runs a skill. On 'none', pick the skill the normal way.
    """
    import skill_router

    d = skill_router.route_skill(request, skills)
    return SkillRouteDecision(
        selected=d["selected"], proposed=d.get("proposed"), confidence=d["confidence"],
        reason=d["reason"], source=d["source"], note=skill_router.note(d),
        latency_ms=d["latency_ms"],
    )


# ------------------------------------------------------------------- server


class TokenAuth(BaseHTTPMiddleware):
    """
    Shared-secret gate.

    These tools spend a Jev quota, so an open endpoint is someone else's bill.
    Accepts `Authorization: Bearer <token>`, `X-Api-Key: <token>`, or a
    `/t/<token>` URL prefix for orgs without the request-headers beta.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/health", "/"):
            return await call_next(request)
        if not TOKEN:
            return JSONResponse(
                {"error": "Server misconfigured: JEV_REMOTE_TOKEN is unset. Refusing to "
                          "serve tools that spend an API quota without authentication."},
                status_code=503,
            )

        auth = request.headers.get("authorization", "")
        presented = (
            auth[7:].strip() if auth.lower().startswith("bearer ") else
            auth.strip() if auth else
            request.headers.get("x-api-key", "").strip()
        )
        if not presented:
            presented = getattr(request.state, "path_token", "")

        # Constant-time compare — the token is short and guessable byte by byte otherwise.
        import hmac
        if not hmac.compare_digest(presented, TOKEN):
            return JSONResponse(
                {"error": "Unauthorized. Configure the connector's Request headers with "
                          "authorization = 'Bearer <token>', or use the /t/<token>/mcp URL form."},
                status_code=401,
            )
        return await call_next(request)


class PathToken(BaseHTTPMiddleware):
    """Strip a `/t/<token>` prefix and hand it to TokenAuth."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/t/"):
            parts = path.split("/", 3)
            if len(parts) >= 4:
                request.state.path_token = parts[2]
                request.scope["path"] = "/" + parts[3]
        return await call_next(request)


async def health(_: Request) -> PlainTextResponse:
    ready = "ok" if active_transport() else "missing OPENROUTER_API_KEY / TYPESAFE_API_KEY"
    auth = "token set" if TOKEN else "NO TOKEN — server will refuse requests"
    return PlainTextResponse(f"jev-remote: {ready}; auth: {auth}\n")


def build_app():
    """ASGI app: MCP at /mcp, plus an unauthenticated /health."""
    # The SDK defaults to localhost-only Host validation, which rejects the
    # public hostname this runs under. DNS-rebinding protection guards servers
    # on a user's own machine from hostile web pages; a public HTTPS endpoint
    # has no such exposure, and TokenAuth is what gates it.
    from mcp.server.transport_security import TransportSecuritySettings
    app = mcp.streamable_http_app(
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.router.routes.append(Route("/health", health, methods=["GET"]))
    app.add_middleware(TokenAuth)
    app.add_middleware(PathToken)     # runs first: strips the prefix before auth
    return app


app = build_app()


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    if not TOKEN:
        print("WARNING: JEV_REMOTE_TOKEN unset — every request will be refused.", file=sys.stderr)
    uvicorn.run(app, host="0.0.0.0", port=port)
