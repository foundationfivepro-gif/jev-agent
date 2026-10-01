"""Prepared, opt-in synthetic OpenRouter pilot. NO network or key reader exists.

This module is separate from the mock-only MCP/writing service. A future trusted
operator must supply authorization, fresh catalog and a secure bound transport.
No CLI, environment flag, request field or synthetic fixture enables live I/O.
See LIVE_PILOT.md for the remaining activation gates and measurement limitations.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Callable, Protocol

from routing_policy import RoutingPolicyError, jev_selection_scope

VERSION = "synthetic-paired-jev-v2"
CAP_USD = Decimal("5")
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
JEV_ENDPOINT = "https://openrouter.ai/api/v1/systemone"
JEV_MODEL = "jev-latest"
ALIASES = {"routine": "~anthropic/claude-sonnet-latest",
           "complex": "~anthropic/claude-opus-latest"}
SURFACES = ("codex-profile-surrogate", "dot-profile-surrogate")
_NANO = Decimal("1000000000")
_ID = re.compile(r"^[A-Za-z0-9_~./:-]{1,160}$")
_SECRET = re.compile(r"(?:sk-|ghp_|xox[baprs]-)[A-Za-z0-9_-]{8,}|AKIA[A-Z0-9]{16}"
                     r"|eyJ[\w-]{10,}\.[\w-]+\.[\w-]+|CANARY_SECRET"
                     r"|(?:api[_-]?key|password|client_secret|access_token|bearer)[:=]", re.I)
_ERROR_CODES = frozenset({"invalid_money", "invalid_or_expired_authorization",
    "invalid_catalog_or_privacy", "invalid_usage", "durable_ledger_required",
    "invalid_cap", "ledger_authorization_mismatch", "missing_reservation",
    "catalog_expired", "actual_target_unverified", "invalid_output",
    "invalid_finish_reason", "transport_deadline_exceeded", "authorization_changed",
    "unknown_transport_evidence", "invalid_routing_plan", "invalid_routing_receipt",
    "routing_constraint_violation", "routing_decision_expired",
    "routing_deadline_exceeded"})


class PilotError(Exception):
    """Sanitized public status: do not wrap provider exceptions or credentials."""
    def __init__(self, code: str):
        self.code = code if type(code) is str and code in _ERROR_CODES else "preflight_unavailable"
        super().__init__(self.code)


def _error_code(error: PilotError) -> str:
    # Do not call a supplied exception subclass's __str__ implementation.
    return error.code if type(error) is PilotError else "preflight_unavailable"


def _money(value: str | Decimal) -> Decimal:
    try:
        if not isinstance(value, (str, Decimal)):
            raise ValueError
        result = Decimal(value)
        if not result.is_finite() or result < 0 or result > 1_000_000:
            raise ValueError
        return result
    except (ValueError, InvalidOperation):
        raise PilotError("invalid_money") from None


def _nanos(value: str | Decimal) -> int:
    return int((_money(value) * _NANO).to_integral_value(rounding=ROUND_CEILING))


def _usd(value: int) -> str:
    return format(Decimal(value) / _NANO, "f")


def _identifier(value: str) -> bool:
    return type(value) is str and bool(_ID.fullmatch(value)) and not _SECRET.search(value)


def _finite(value: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class Brief:
    id: str
    complexity: str
    instruction: str
    relevant: tuple[str, ...]
    distractors: tuple[str, ...]
    required: tuple[str, ...]


# All content is checked-in synthetic fixture text. No user-supplied text/path.
BRIEFS = (
    Brief("launch-note", "routine", "Write a friendly announcement in at most 70 words.",
          ("Fictional product: Lantern.", "Release day: Tuesday.", "The release adds offline search."),
          ("Old office chairs were blue.", "The fictional cafeteria sells soup.",
           "An archived prototype was called Pebble.") * 8,
          ("lantern", "tuesday", "offline search")),
    Brief("tradeoff-summary", "complex", "Write a balanced recommendation in at most 100 words.",
          ("Fictional teams compare option Cedar with option Maple.",
           "Cedar costs 10 credits and takes 3 days; Maple costs 6 credits and takes 7 days.",
           "The deadline is 5 days; recommend Cedar and explain the cost tradeoff."),
          ("An unrelated exercise used option Birch.", "The imaginary stationery was green.",
           "Last year's fictional picnic was indoors.") * 8,
          ("cedar", "maple", "10", "6", "3", "7", "5")),
)


@dataclass(frozen=True)
class Approval:
    """Only a trusted operator authorizer may supply this, never a tool body.

    The approval ID identifies ONE shared ledger across surfaces and restarts.
    Dollar approval alone does not imply provider/content/credential approval.
    """
    approval_id: str
    cap_usd: str
    allowed_providers: frozenset[str]
    expires_at: float
    synthetic_only: bool = True
    endpoint: str = ENDPOINT
    max_output_tokens: int = 256
    timeout_seconds: float = 30.0
    max_attempts: int = 2
    # Approval of generation/spend alone does not authorize a JEV request.
    jev_routing_approved: bool = False

    def validate(self, now: float) -> None:
        if (not _identifier(self.approval_id) or not 0 < _money(self.cap_usd) <= CAP_USD
                or not self.allowed_providers or any(not _identifier(p) for p in self.allowed_providers)
                or self.synthetic_only is not True or self.endpoint != ENDPOINT
                or self.jev_routing_approved is not True
                or not _finite(self.expires_at) or self.expires_at <= now
                or type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 512
                or not _finite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 60
                or type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 2):
            raise PilotError("invalid_or_expired_authorization")


@dataclass(frozen=True)
class Target:
    alias: str
    model: str
    provider: str
    catalog_version: str
    observed_at: float
    expires_at: float
    # USD per token, not per million. Include request fees and worst cache price.
    input_price: str
    output_price: str
    cache_read_price: str
    cache_write_price: str
    request_price: str
    max_output_tokens: int
    context_tokens: int
    # Catalog adapter must provide a proved tokenizer/template upper bound, not
    # a chars/4 heuristic. Refuse a target if such a bound cannot be established.
    input_token_upper_bound: int
    stable: bool
    data_collection_deny: bool
    zero_data_retention: bool
    supports_required_parameters: bool
    family: str

    def validate(self, alias: str, approval: Approval, now: float) -> None:
        if (alias not in ALIASES.values() or self.alias != alias or not all(_identifier(v) for v in
                (self.model, self.provider, self.catalog_version))
                or not self.model.startswith("anthropic/")
                or self.family != ("sonnet" if alias == ALIASES["routine"] else "opus")
                or not re.search(r"(?:^|[-_/])" + re.escape(self.family) + r"(?:[-_/]|$)", self.model)
                or self.provider not in approval.allowed_providers
                or not _finite(self.observed_at) or not _finite(self.expires_at)
                or not self.observed_at <= now < self.expires_at
                or now - self.observed_at > 300
                or any(v is not True for v in (self.stable, self.data_collection_deny,
                       self.zero_data_retention, self.supports_required_parameters))
                or any(type(v) is not int or v <= 0 for v in
                       (self.max_output_tokens, self.context_tokens, self.input_token_upper_bound))
                or self.max_output_tokens < approval.max_output_tokens
                or self.input_token_upper_bound + approval.max_output_tokens > self.context_tokens):
            raise PilotError("invalid_catalog_or_privacy")
        for value in (self.input_price, self.output_price, self.cache_read_price,
                      self.cache_write_price, self.request_price):
            _money(value)

    def ceiling(self, output_tokens: int) -> Decimal:
        # Conservatively count cache read/write as additive even if a provider
        # includes them in prompt billing. No planned discount is assumed.
        return (self.input_token_upper_bound * (_money(self.input_price)
                + _money(self.cache_read_price) + _money(self.cache_write_price))
                + output_tokens * _money(self.output_price) + _money(self.request_price))


class Catalog(Protocol):
    # Resolve metadata ONLY for JEV's exact selected model; never select a model.
    def resolve(self, model: str, *, alias: str, prompt: str, max_output_tokens: int) -> Target: ...


@dataclass(frozen=True)
class Usage:
    # Input includes cached input; output includes reasoning. Details are not
    # added a second time. The reviewed adapter must verify provider semantics.
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    def validate(self, target: Target, output_limit: int) -> None:
        self.validate_limits(target.input_token_upper_bound, output_limit)

    def validate_limits(self, input_limit: int, output_limit: int) -> None:
        if (any(type(v) is not int or v < 0 for v in asdict(self).values())
                or self.input_tokens > input_limit
                or self.output_tokens > output_limit
                or self.cache_read_tokens + self.cache_write_tokens > self.input_tokens
                or self.reasoning_tokens > self.output_tokens):
            raise PilotError("invalid_usage")


@dataclass(frozen=True)
class RoutingRequest:
    """Only trusted, checked-in synthetic tasks and hard model constraints."""
    requested_alias: str
    required_family: str
    prompt: str = field(repr=False)
    max_output_tokens: int
    allowed_providers: tuple[str, ...]
    required_model: str | None = None

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class RoutingPlan:
    """A non-billable preflight ceiling, including ALL routing overhead/fees."""
    plan_id: str
    max_cost_usd: str
    input_token_upper_bound: int
    max_output_tokens: int
    observed_at: float
    expires_at: float
    evidence_mode: str
    endpoint: str = JEV_ENDPOINT
    model: str = JEV_MODEL

    def validate(self, approval: Approval, now: float) -> None:
        if (not _identifier(self.plan_id) or type(self.max_cost_usd) is not str
                or type(self.endpoint) is not str or self.endpoint != JEV_ENDPOINT
                or type(self.model) is not str or self.model != JEV_MODEL
                or type(self.evidence_mode) is not str or self.evidence_mode not in ("fixture", "live")
                or not _finite(self.observed_at) or not _finite(self.expires_at)
                or not self.observed_at <= now < self.expires_at
                or now - self.observed_at > 300
                or type(self.input_token_upper_bound) is not int or self.input_token_upper_bound <= 0
                or type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 512
                or _money(self.max_cost_usd) > _money(approval.cap_usd)):
            raise PilotError("invalid_routing_plan")
        # A purported free live request has no enforceable billable ceiling.
        if self.evidence_mode == "live" and _money(self.max_cost_usd) == 0:
            raise PilotError("invalid_routing_plan")


@dataclass(frozen=True)
class RoutingReceipt:
    decision_id: str
    request_sha256: str
    selected_model: str
    selected_family: str
    usage: Usage
    billed_usd: str
    actual_router_model: str
    evidence_mode: str

    def validate(self, request: RoutingRequest, plan: RoutingPlan) -> None:
        if (not all(_identifier(v) for v in (self.decision_id, self.selected_model, self.selected_family))
                or type(self.request_sha256) is not str or self.request_sha256 != request.fingerprint()
                or type(self.actual_router_model) is not str or self.actual_router_model != JEV_MODEL
                or type(self.evidence_mode) is not str or self.evidence_mode != plan.evidence_mode
                or type(self.usage) is not Usage):
            raise PilotError("invalid_routing_receipt")
        self.usage.validate_limits(plan.input_token_upper_bound, plan.max_output_tokens)
        if (self.selected_family != request.required_family
                or not self.selected_model.startswith("anthropic/")
                or not re.search(r"(?:^|[-_/])" + re.escape(request.required_family)
                                 + r"(?:[-_/]|$)", self.selected_model)
                or (request.required_model is not None and self.selected_model != request.required_model)):
            raise PilotError("routing_constraint_violation")


class JevRouter(Protocol):
    """Trusted operator injection, never request data or the general JEV SDK.

    prepare is local/non-billable and cannot read/bind credentials or make I/O.
    decide makes exactly one bounded, nonrecursive JEV request using preexisting
    secure injection, the plan's fixed host/model, disabled retries/fallbacks,
    and a real deadline. All billable work must fit the reserved ceiling. It
    returns definitive native usage/billing; an unavailable receipt is unknown.
    JEV itself is the routing primitive and is never routed through itself.
    """
    def prepare(self, request: RoutingRequest, approval: Approval) -> RoutingPlan: ...

    def decide(self, *, request: RoutingRequest, plan: RoutingPlan, approval: Approval,
               timeout_seconds: float, operation_id: str) -> RoutingReceipt | Failure: ...


@dataclass(frozen=True)
class Reply:
    generation_id: str
    actual_model: str
    actual_provider: str
    usage: Usage
    billed_usd: str  # Definitive observed charge; unknown/estimate is not valid.
    finish_reason: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class Failure:
    # A timeout/429/5xx alone never proves absence of a charge.
    definitely_not_sent: bool = False
    charge_impossible: bool = False


class BoundTransport(Protocol):
    """Trusted adapter must enforce deadline/size and disable SDK auto-retries.

    The binding owns credentials and never exposes them to this module. It must
    not log headers/body, change destinations, or invent usage/cost receipts.
    """
    evidence_mode: str  # "fixture" or "live"; never inferred from a model name.

    def send(self, *, endpoint: str, payload: dict, timeout_seconds: float,
             operation_id: str) -> Reply | Failure: ...


class SecureTransportBinding(Protocol):
    def bind_preexisting(self, approval: Approval) -> BoundTransport:
        """Bind approved existing secure injection; do not provision/read keys."""
        ...


class PilotLedger:
    """Durable cross-process, single-flight, single-approval budget and receipts.

    Run against a trusted canonical local SQLite path; never clone/reset it for a
    repeated run. Pending/unknown reservations survive a crash and block replay.
    No raw prompts, generated text, provider exceptions or secrets are stored.
    """
    def __init__(self, path: Path, approval: Approval):
        if str(path) == ":memory:":
            raise PilotError("durable_ledger_required")
        self.path = str(path)
        self.approval_id = approval.approval_id
        if not _identifier(self.approval_id):
            raise PilotError("invalid_or_expired_authorization")
        cap = _nanos(approval.cap_usd)
        if not 0 < cap <= _nanos(CAP_USD):
            raise PilotError("invalid_cap")
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS pilot (id INTEGER PRIMARY KEY CHECK(id=1), approval TEXT, cap INTEGER, halted TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, fingerprint TEXT, reserved INTEGER, charged INTEGER, status TEXT, receipt TEXT)")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT approval, cap FROM pilot WHERE id=1").fetchone()
            if row is None:
                db.execute("INSERT INTO pilot VALUES (1, ?, ?, '')", (approval.approval_id, cap))
            elif row != (approval.approval_id, cap):
                raise PilotError("ledger_authorization_mismatch")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, operation: str, fingerprint: str, ceiling: Decimal) -> dict:
        amount = _nanos(ceiling)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint, status, receipt FROM operations WHERE id=?", (operation,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    return {"status": "idempotency_conflict"}
                return json.loads(row[2]) if row[2] else {"status": "operation_pending_no_replay"}
            cap, halted = db.execute("SELECT cap, halted FROM pilot WHERE id=1").fetchone()
            if halted:
                return {"status": halted}
            if db.execute("SELECT 1 FROM operations WHERE status='pending'").fetchone():
                return {"status": "operation_pending_no_replay"}
            committed = db.execute("SELECT COALESCE(SUM(reserved + charged), 0) FROM operations").fetchone()[0]
            if committed + amount > cap:
                db.execute("UPDATE pilot SET halted='budget_exhausted' WHERE id=1")
                return {"status": "budget_exhausted"}
            db.execute("INSERT INTO operations VALUES (?, ?, ?, 0, 'pending', NULL)", (operation, fingerprint, amount))
            return {"status": "claimed"}

    def finish(self, operation: str, result: dict, *, billed: str | None, halt: str = "") -> dict:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reserved, status, receipt FROM operations WHERE id=?", (operation,)).fetchone()
            if row is None:
                raise PilotError("missing_reservation")
            if row[1] != "pending":
                return json.loads(row[2])
            charge = _nanos(billed) if billed is not None else None
            if charge is not None and charge > row[0]:
                halt = "charge_exceeded_reservation"
                result = {**result, "status": halt}
            # Unknown cost holds full reservation and halts the entire pilot.
            if charge is None:
                halt = halt or "uncertain_charge"
            db.execute("UPDATE operations SET reserved=?, charged=?, status=?, receipt=? WHERE id=?",
                       (row[0] if charge is None else 0, charge or 0,
                        "unknown" if charge is None else "settled", json.dumps(result, sort_keys=True), operation))
            if halt:
                db.execute("UPDATE pilot SET halted=? WHERE id=1", (halt,))
        return result

    def snapshot(self) -> dict:
        with self._db() as db:
            cap, halted = db.execute("SELECT cap, halted FROM pilot WHERE id=1").fetchone()
            reserved, charged = db.execute("SELECT COALESCE(SUM(reserved),0), COALESCE(SUM(charged),0) FROM operations").fetchone()
            receipts = [json.loads(r[0]) for r in db.execute("SELECT receipt FROM operations WHERE receipt IS NOT NULL ORDER BY id")]
        return {"cap_usd": _usd(cap), "reserved_usd": _usd(reserved), "charged_usd": _usd(charged),
                "halted": halted or None, "receipts": receipts}


class LivePilot:
    def __init__(self, *, ledger: PilotLedger | None = None, catalog: Catalog | None = None,
                 authorizer: Callable[[], Approval] | None = None,
                 secure_binding: SecureTransportBinding | None = None,
                 jev_router: JevRouter | None = None,
                 clock: Callable[[], float] = time.time):
        self.ledger, self.catalog = ledger, catalog
        self.authorizer, self.secure_binding, self.clock = authorizer, secure_binding, clock
        self.jev_router = jev_router

    @staticmethod
    def prompt(brief: Brief, arm: str) -> str:
        facts = brief.relevant + (brief.distractors if arm == "baseline" else ())
        return "SYNTHETIC FIXTURE ONLY.\n" + brief.instruction + "\n" + "\n".join(facts)

    def _route(self, request: RoutingRequest, approval: Approval, operation: str) -> dict:
        """One auditable routing operation in the generation's SAME ledger."""
        try:
            plan = self.jev_router.prepare(request, approval)
            if type(plan) is not RoutingPlan:
                raise PilotError("invalid_routing_plan")
            plan.validate(approval, self.clock())
            fingerprint = hashlib.sha256(json.dumps({"request": request.fingerprint(),
                "plan": {k: v for k, v in asdict(plan).items() if k not in ("observed_at", "expires_at")},
                "approval": {**asdict(approval), "allowed_providers": sorted(approval.allowed_providers)}},
                sort_keys=True).encode()).hexdigest()
            claim = self.ledger.claim(operation, fingerprint, _money(plan.max_cost_usd))
            if claim["status"] != "claimed":
                if claim["status"] == "routed" and self.clock() >= claim["expires_at"]:
                    return {**claim, "status": "routing_decision_expired"}
                return claim
        except PilotError as error:
            return {"status": _error_code(error)}
        except Exception:
            return {"status": "routing_preflight_unavailable"}
        base = {"operation_id": operation, "mode": "jev_required",
                "requested_alias": request.requested_alias, "required_family": request.required_family,
                "required_model": request.required_model, "request_sha256": request.fingerprint(),
                "routing_endpoint": plan.endpoint, "routing_model": plan.model,
                "plan_id": plan.plan_id, "expires_at": plan.expires_at,
                "reserved_upper_usd": plan.max_cost_usd, "evidence_mode": plan.evidence_mode}
        try:
            latest = self.authorizer()
            latest.validate(self.clock())
            if latest != approval:
                raise PilotError("authorization_changed")
            plan.validate(latest, self.clock())
        except Exception as error:
            status = _error_code(error) if isinstance(error, PilotError) else "authorization_unavailable"
            return self.ledger.finish(operation, {**base, "status": status, "billed_usd": "0"}, billed="0", halt=status)
        before = self.clock()
        try:
            with jev_selection_scope():
                reply = self.jev_router.decide(request=request, plan=plan, approval=approval,
                    timeout_seconds=approval.timeout_seconds, operation_id=operation)
        except RoutingPolicyError:
            return self.ledger.finish(operation, {**base, "status": "recursive_routing_blocked"},
                                      billed=None, halt="recursive_routing_blocked")
        except Exception:
            reply = Failure()
        elapsed = self.clock() - before
        if type(reply) is Failure and reply.definitely_not_sent is True and reply.charge_impossible is True:
            return self.ledger.finish(operation, {**base, "status": "routing_not_sent", "billed_usd": "0"},
                                      billed="0", halt="routing_not_sent")
        if type(reply) is not RoutingReceipt:
            return self.ledger.finish(operation, {**base, "status": "uncertain_routing_charge"},
                                      billed=None, halt="uncertain_routing_charge")
        try:
            billed = str(_money(reply.billed_usd))
        except PilotError:
            return self.ledger.finish(operation, {**base, "status": "uncertain_routing_charge"},
                                      billed=None, halt="uncertain_routing_charge")
        base["billed_usd"] = billed
        # Preserve safe rejected selections for audit, without persisting an
        # arbitrary provider object, secret-shaped metadata, or raw rationale.
        for key in ("decision_id", "selected_model", "selected_family", "actual_router_model"):
            value = getattr(reply, key)
            if _identifier(value):
                base[key] = value
        try:
            if type(reply.usage) is Usage:
                reply.usage.validate_limits(plan.input_token_upper_bound, plan.max_output_tokens)
                base["usage"] = asdict(reply.usage)
            reply.validate(request, plan)
            if not _finite(elapsed) or elapsed < 0 or elapsed > approval.timeout_seconds:
                raise PilotError("routing_deadline_exceeded")
        except PilotError as error:
            return self.ledger.finish(operation, {**base, "status": _error_code(error)},
                                      billed=billed, halt=_error_code(error))
        result = {**base, "status": "routed", "decision_id": reply.decision_id,
                  "selected_model": reply.selected_model, "selected_family": reply.selected_family,
                  "actual_router_model": reply.actual_router_model, "usage": asdict(reply.usage),
                  "latency_seconds": elapsed, "cost_kind": "billed"}
        return self.ledger.finish(operation, result, billed=billed)

    def run_one(self, surface: str, brief_id: str, arm: str) -> dict:
        if any(x is None for x in (self.ledger, self.catalog, self.authorizer, self.secure_binding)):
            return {"status": "live_adapter_unprovisioned"}
        if self.jev_router is None:
            return {"status": "jev_router_unprovisioned"}
        if surface not in SURFACES or arm not in ("baseline", "optimized"):
            return {"status": "unknown_synthetic_case"}
        brief = next((b for b in BRIEFS if b.id == brief_id), None)
        if brief is None:
            return {"status": "unknown_synthetic_case"}
        decision = None
        try:
            approval = self.authorizer()
            approval.validate(self.clock())
            if self.ledger.approval_id != approval.approval_id:
                raise PilotError("ledger_authorization_mismatch")
            # Cap changes cannot silently broaden the already bound ledger.
            if _money(approval.cap_usd) != _money(self.ledger.snapshot()["cap_usd"]):
                raise PilotError("ledger_authorization_mismatch")
            prompt = self.prompt(brief, arm)
            alias = ALIASES[brief.complexity]
            operation = ":".join((VERSION, surface, brief.id, arm))
            request = RoutingRequest(alias, "sonnet" if brief.complexity == "routine" else "opus", prompt,
                                     approval.max_output_tokens, tuple(sorted(approval.allowed_providers)))
            decision = self._route(request, approval, operation + ":jev")
            if decision["status"] != "routed":
                return {"status": decision["status"], "decision": decision}
            target = self.catalog.resolve(decision["selected_model"], alias=alias,
                                          prompt=prompt, max_output_tokens=approval.max_output_tokens)
            if type(target) is not Target or target.model != decision["selected_model"]:
                raise PilotError("routing_constraint_violation")
            target.validate(alias, approval, self.clock())
            payload = {"model": target.model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": approval.max_output_tokens, "stream": False,
                       "provider": {"only": [target.provider], "order": [target.provider],
                           "allow_fallbacks": False, "require_parameters": True,
                           "data_collection": "deny", "zdr": True,
                           "max_price": {"prompt": str(_money(target.input_price) * 1_000_000),
                                         "completion": str(_money(target.output_price) * 1_000_000),
                                         "request": target.request_price}}}
            fingerprint = hashlib.sha256(json.dumps({"payload": payload, "target":
                {k: v for k, v in asdict(target).items() if k not in ("observed_at", "expires_at")},
                "jev_decision_id": decision["decision_id"], "routing_request": decision["request_sha256"],
                "timeout": approval.timeout_seconds, "attempts": approval.max_attempts}, sort_keys=True).encode()).hexdigest()
            claim = self.ledger.claim(operation, fingerprint, target.ceiling(approval.max_output_tokens))
            if claim["status"] != "claimed":
                return claim if "decision" in claim else {**claim, "decision": decision}
        except PilotError as error:
            return {"status": _error_code(error), **({"decision": decision} if decision else {})}
        except Exception:
            return {"status": "preflight_unavailable", **({"decision": decision} if decision else {})}

        base = {"operation_id": operation, "surface": surface, "brief_id": brief.id,
                "arm": arm, "measurement_scope": "synthetic_api_surrogate", "requested_alias": alias,
                "planned_model": target.model, "planned_provider": target.provider,
                "catalog_version": target.catalog_version, "catalog_observed_at": target.observed_at,
                "target_family": target.family,
                "pricing": {k: v for k, v in asdict(target).items() if k.endswith("_price")},
                "reserved_upper_usd": str(target.ceiling(approval.max_output_tokens)),
                "decision": decision,
                "tools": {"mode": "local_only", "tokens": 0, "cost_usd": "0"},
                "parent_overhead": {"tokens": None, "cost_usd": None, "status": "unobserved"},
                "host_runtime": "unrun", "attempts": []}
        started = self.clock()
        try:
            # No credential callback is touched until all preceding checks pass.
            transport = self.secure_binding.bind_preexisting(approval)
            if transport.evidence_mode not in ("fixture", "live"):
                raise PilotError("unknown_transport_evidence")
            base["provider_execution_evidence"] = transport.evidence_mode
        except Exception:
            return self.ledger.finish(operation, {**base, "status": "secure_binding_unavailable"}, billed="0", halt="secure_binding_unavailable")
        if transport.evidence_mode != decision["evidence_mode"]:
            return self.ledger.finish(operation, {**base, "status": "routing_evidence_mismatch"},
                                      billed="0", halt="routing_evidence_mismatch")
        for attempt in range(1, approval.max_attempts + 1):
            try:
                # Revoke/withdrawal is authoritative even between safe retries.
                latest = self.authorizer()
                latest.validate(self.clock())
                if latest != approval:
                    raise PilotError("authorization_changed")
                if self.clock() >= decision["expires_at"]:
                    raise PilotError("routing_decision_expired")
                target.validate(alias, latest, self.clock())
                if self.clock() >= target.expires_at:
                    raise PilotError("catalog_expired")
            except PilotError as error:
                return self.ledger.finish(operation, {**base, "status": _error_code(error)}, billed="0", halt=_error_code(error))
            except Exception:
                return self.ledger.finish(operation, {**base, "status": "authorization_unavailable"}, billed="0", halt="authorization_unavailable")
            before = self.clock()
            try:
                reply = transport.send(endpoint=ENDPOINT, payload=payload,
                                       timeout_seconds=approval.timeout_seconds, operation_id=operation)
            except Exception:
                # Exceptions may contain secrets; discard without formatting.
                reply = Failure()
            elapsed = self.clock() - before
            no_charge = (type(reply) is Failure and reply.definitely_not_sent is True
                         and reply.charge_impossible is True)
            base["attempts"].append({"number": attempt, "latency_seconds": max(0, elapsed),
                                     "known_no_charge": no_charge, "retry": attempt > 1,
                                     "billed_usd": "0" if no_charge else None,
                                     "input_tokens": 0 if no_charge else None,
                                     "output_tokens": 0 if no_charge else None})
            if no_charge:
                if attempt < approval.max_attempts:
                    continue
                return self.ledger.finish(operation, {**base, "status": "not_sent_retry_limit"}, billed="0")
            if type(reply) is not Reply:
                return self.ledger.finish(operation, {**base, "status": "uncertain_charge"}, billed=None)
            try:
                billed = str(_money(reply.billed_usd))
            except PilotError:
                return self.ledger.finish(operation, {**base, "status": "uncertain_charge"}, billed=None)
            try:
                if (not _identifier(reply.generation_id) or reply.actual_model != target.model
                        or reply.actual_provider != target.provider):
                    raise PilotError("actual_target_unverified")
                if type(reply.usage) is not Usage:
                    raise PilotError("invalid_usage")
                reply.usage.validate(target, approval.max_output_tokens)
                if not isinstance(reply.text, str) or len(reply.text.encode()) > 65_536:
                    raise PilotError("invalid_output")
                if reply.finish_reason not in ("stop", "length", "refusal"):
                    raise PilotError("invalid_finish_reason")
                if not _finite(elapsed) or elapsed < 0 or elapsed > approval.timeout_seconds:
                    raise PilotError("transport_deadline_exceeded")
            except PilotError as error:
                return self.ledger.finish(operation, {**base, "status": _error_code(error), "billed_usd": billed}, billed=billed, halt=_error_code(error))
            acceptance = (reply.finish_reason == "stop" and all(t in reply.text.lower() for t in brief.required)
                          and len(reply.text.split()) <= (70 if brief.complexity == "routine" else 100))
            base["attempts"][-1].update({"billed_usd": billed, **asdict(reply.usage)})
            result = {**base, "status": "completed", "generation_id": reply.generation_id,
                      "actual_model": reply.actual_model, "actual_provider": reply.actual_provider,
                      "usage": asdict(reply.usage), "billed_usd": billed, "cost_kind": "billed",
                      "latency_seconds": max(0, self.clock() - started), "finish_reason": reply.finish_reason,
                      "retry_count": attempt - 1, "accepted": acceptance,
                      "acceptance_kind": "deterministic_fixture_checks_only",
                      "output_sha256": hashlib.sha256(reply.text.encode()).hexdigest()}
            return self.ledger.finish(operation, result, billed=billed)
        raise AssertionError("bounded loop invariant")

    def run_predefined_pairs(self) -> dict:
        """Eight generation cases at most; all share the same approval/ledger.

        Counterbalance arm order across fixtures; no adaptive quality retries.
        Reinvocation returns durable existing receipts and never replays a call.
        """
        results = []
        for surface in SURFACES:
            for index, brief in enumerate(BRIEFS):
                for arm in (("baseline", "optimized") if index % 2 == 0 else ("optimized", "baseline")):
                    result = self.run_one(surface, brief.id, arm)
                    results.append(result)
                    if result["status"] != "completed":
                        return self.report(results)
        return self.report(results)

    def report(self, results: list[dict]) -> dict:
        pairs = []
        for surface in SURFACES:
            for brief in BRIEFS:
                arms = {r.get("arm"): r for r in results if r.get("surface") == surface
                        and r.get("brief_id") == brief.id and r.get("status") == "completed"}
                if set(arms) != {"baseline", "optimized"}:
                    continue
                baseline, optimized = arms["baseline"], arms["optimized"]
                comparable = all(baseline.get(key) == optimized.get(key) for key in
                    ("actual_model", "actual_provider", "target_family", "catalog_version", "pricing", "provider_execution_evidence"))
                comparable = comparable and all(baseline["decision"].get(key) == optimized["decision"].get(key)
                    for key in ("actual_router_model", "plan_id", "evidence_mode"))
                b_tokens = baseline["usage"]["input_tokens"] + baseline["usage"]["output_tokens"]
                o_tokens = optimized["usage"]["input_tokens"] + optimized["usage"]["output_tokens"]
                b_tokens += baseline["decision"]["usage"]["input_tokens"] + baseline["decision"]["usage"]["output_tokens"]
                o_tokens += optimized["decision"]["usage"]["input_tokens"] + optimized["decision"]["usage"]["output_tokens"]
                pairs.append({"surface": surface, "brief_id": brief.id,
                              "api_subtotals_comparable": comparable,
                              "incomparability_reason": None if comparable else "model_provider_catalog_pricing_or_execution_changed",
                              "both_pass_fixture_checks": baseline["accepted"] and optimized["accepted"],
                              "api_subtotal_token_delta_baseline_minus_optimized": b_tokens - o_tokens if comparable else None,
                              "api_subtotal_usd_delta_baseline_minus_optimized": str(
                                  _money(baseline["billed_usd"]) + _money(baseline["decision"]["billed_usd"])
                                  - _money(optimized["billed_usd"]) - _money(optimized["decision"]["billed_usd"])) if comparable else None,
                              "net_workflow_token_savings": None,
                              "net_workflow_cost_savings": None})
        accepted = sum(r.get("accepted") is True for r in results)
        generation_cost = sum((_money(r["billed_usd"]) for r in results if "billed_usd" in r), Decimal(0))
        routing_cost = sum((_money(r["decision"]["billed_usd"]) for r in results
                            if "billed_usd" in r.get("decision", {})), Decimal(0))
        known_cost = generation_cost + routing_cost
        all_costs_known = all(("billed_usd" in r or r.get("status") in
                             ("not_sent_retry_limit", "secure_binding_unavailable"))
                             and "billed_usd" in r.get("decision", {}) for r in results)
        routing_evidence = {r["decision"].get("evidence_mode") for r in results
                            if r.get("decision", {}).get("status") == "routed"}
        return {"version": VERSION, "measurement_scope": "synthetic_api_surrogate",
                "actual_codex_runtime": "unrun", "actual_dot_runtime": "unrun",
                "actual_jev_decision_runtime": "unrun" if "live" not in routing_evidence else "live_api_receipts_only",
                "jev_routing_evidence": sorted(routing_evidence), "net_savings_claim": "unavailable",
                "limitations": ["Parent coordination/rereading tokens and charges are unobserved",
                                "API profiles do not measure Codex/dot host executions or subscription costs",
                                "Fixture JEV receipts do not measure live routing; live API receipts do not measure hosts",
                                "Deterministic fixture checks are not blinded human quality assessment",
                                "Two artificial briefs do not establish universal savings"],
                "accepted_fixture_tasks": accepted,
                "known_generation_subtotal_billed_usd": str(generation_cost),
                "known_routing_subtotal_billed_usd": str(routing_cost),
                "known_api_subtotal_billed_usd": str(known_cost),
                "api_cost_per_accepted_fixture_task_usd": str(known_cost / accepted)
                    if accepted and all_costs_known else None,
                "all_workflow_costs_observed": False,
                "pairs": pairs, "results": results,
                "budget": self.ledger.snapshot() if self.ledger else None}
