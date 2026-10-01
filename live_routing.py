"""Explicit live composition root. No environment/key loading or implicit activation.

Only trusted host setup supplies authorization, catalog discovery, transport,
ledger and dispatcher. MCP arguments cannot install any of these dependencies.
"""
from __future__ import annotations

import json
import hashlib
import math
from dataclasses import dataclass
import time
import urllib.request
from decimal import Decimal
from threading import Lock

from host_adapters import TrustedJevSelector, recommend_route
from host_contracts import CapabilitySnapshot, JevRouteDecision, RouteCandidate, RoutingUsage, TaskEnvelope
from live_pilot import PilotLedger, _money
from privacy import screen_outbound

ENDPOINT = "https://openrouter.ai/api/v1/systemone"
MODEL = "jev-latest"


class LiveRoutingError(ValueError):
    def __init__(self):
        super().__init__("live_routing_unavailable")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _ForcedProxy(urllib.request.ProxyHandler):
    """Never allow NO_PROXY to bypass the protected credential proxy."""
    def __init__(self, proxies, *, allowed_urls=None):
        self.allowed_urls = frozenset(allowed_urls or {ENDPOINT})
        super().__init__(proxies)

    def proxy_open(self, request, proxy, scheme):
        from urllib.parse import urlsplit
        parsed = urlsplit(proxy)
        if request.full_url not in self.allowed_urls or request.type != "https":
            raise LiveRoutingError()
        request.set_proxy(parsed.netloc, parsed.scheme)
        return None


class ProtectedOpenRouterTransport:
    """Use only an issued placeholder via the host's HTTPS credential proxy.

    The callback must return the issued placeholder, never the raw credential.
    Proxy configuration and placeholder provenance are host responsibilities.
    No legacy environment variables, redirects, retries or exception bodies.
    """
    def __init__(self, *, issued_placeholder, https_proxy: str):
        from urllib.parse import urlsplit
        parsed = urlsplit(https_proxy)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise LiveRoutingError()
        self._placeholder = issued_placeholder
        self._opener = urllib.request.build_opener(
            _ForcedProxy({"https": https_proxy}), _NoRedirects())

    def __call__(self, payload, timeout):
        try:
            encoded = json.dumps(payload, allow_nan=False).encode()
            if len(encoded) > 32768 or not 0 < timeout <= 30:
                raise LiveRoutingError()
            request = urllib.request.Request(ENDPOINT, data=encoded, headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self._placeholder()})
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise LiveRoutingError()
                return json.loads(raw)
        except Exception:
            raise LiveRoutingError() from None


@dataclass(frozen=True)
class RoutingAuthorization:
    """Host-verified consent and cost bound for the exact minimized payload.

    The operator must verify the bound against current System One pricing and
    its input/output limits. A guessed per-call allowance is not a quote.
    """
    payload_sha256: str
    ceiling_usd: str
    expires_at: float
    actual_models: frozenset[str]
    pricing_evidence: str

    def validate(self, payload, now):
        from host_contracts import Identifier
        from pydantic import TypeAdapter
        TypeAdapter(Identifier).validate_python(self.pricing_evidence, strict=True)
        if (self.payload_sha256 != payload_fingerprint(payload)
                or type(self.expires_at) not in (int, float)
                or not math.isfinite(self.expires_at) or now >= self.expires_at
                or not self.actual_models
                or any(not isinstance(m, str) or not m.startswith("typesafe/jev-")
                       or m.endswith("latest") for m in self.actual_models)):
            raise LiveRoutingError()
        return _money(self.ceiling_usd)


def payload_fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


class OpenRouterSelector:
    """One bounded JEV choice, reserved in the canonical ledger before I/O.

    authorize(task, payload) must independently verify consent and return a
    RoutingAuthorization with a verified USD upper bound for this exact request. Unknown charges retain reservations
    and halt the shared ledger. Request IDs cannot trigger another paid attempt.
    """
    def __init__(self, *, transport, ledger: PilotLedger, authorize, clock=time.time):
        self.transport, self.ledger, self.authorize, self.clock = transport, ledger, authorize, clock

    def binding(self):
        return TrustedJevSelector(source_id="openrouter-systemone", evidence_status="jev_observed", select=self)

    def __call__(self, *, task, runtime_id, session_id, request_fingerprint,
                 eligible_models, execution_modes):
        if task.data_class != "public" or "openrouter.ai" not in task.authorized_destinations:
            raise LiveRoutingError()
        choices = {}
        for model in eligible_models:
            for effort in model.efforts or [None]:
                for mode in execution_modes:
                    choices[str(len(choices))] = RouteCandidate(
                        selected_model_id=model.model_id, selected_model_namespace=model.namespace,
                        selected_effort=effort, execution_mode=mode).model_dump()
        if not choices or len(choices) > 256:
            raise LiveRoutingError()
        payload = {"model": MODEL, "state": {
            "purpose": task.purpose, "acceptance_criteria": task.acceptance_criteria,
            "task_kind": task.task_kind, "parent_has_context": task.parent_has_context,
            "inspection_larger_than_result": task.inspection_larger_than_result,
            "failed_acceptance_checks": task.failed_acceptance_checks},
            "questions": {"route": {"type": "choice", "instructions":
                "Select the least costly eligible execution likely to satisfy all acceptance checks. "
                "Prefer inline when the parent holds useful context. All offered constraints are mandatory.",
                "criteria": {key: json.dumps(value, sort_keys=True) for key, value in choices.items()}}}}
        screen_outbound(payload)
        if len(json.dumps(payload).encode()) > 32768:
            raise LiveRoutingError()
        approval = self.authorize(task, payload)
        if type(approval) is not RoutingAuthorization:
            raise LiveRoutingError()
        ceiling = approval.validate(payload, self.clock())
        if ceiling <= 0 or task.budget_usd is None or ceiling > Decimal(str(task.budget_usd)):
            raise LiveRoutingError()
        remaining = min(30.0, min(task.deadline, approval.expires_at) - self.clock())
        if remaining <= 0:
            raise LiveRoutingError()
        operation = "host-route:" + runtime_id + ":" + session_id + ":" + task.request_id
        claim = self.ledger.claim(operation, request_fingerprint, ceiling)
        if claim["status"] != "claimed":
            # Never replay a paid call, including after process restart.
            if (claim["status"] == "routed"
                    and claim.get("actual_router_model") in approval.actual_models):
                cached = JevRouteDecision.model_validate(claim["decision"])
                if cached.cost_usd is not None and Decimal(str(cached.cost_usd)) <= ceiling:
                    return cached
            raise LiveRoutingError()
        billed = None
        try:
            body = self.transport(payload, remaining)
            usage = body["usage"]
            billed = str(_money(str(usage["cost"])))
            if body.get("provider") != "TypeSafe" or body.get("model") not in approval.actual_models:
                raise LiveRoutingError()
            answer = body["answers"]["route"]
            if answer["type"] != "choice" or type(answer["choice"]) is not str:
                raise LiveRoutingError()
            decision = JevRouteDecision(
                decision_id=body["id"], request_id=task.request_id, runtime_id=runtime_id,
                session_id=session_id, request_fingerprint=request_fingerprint,
                candidate=RouteCandidate.model_validate(choices[answer["choice"]]),
                usage=RoutingUsage(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"]),
                cost_usd=float(billed))
            screen_outbound(decision.model_dump())
            if self.clock() >= min(task.deadline, approval.expires_at):
                raise LiveRoutingError()
            result = self.ledger.finish(operation, {"status": "routed", "decision": decision.model_dump(),
                "actual_router_model": body["model"], "endpoint": ENDPOINT,
                "pricing_evidence": approval.pricing_evidence, "payload_sha256": approval.payload_sha256}, billed=billed)
            if result["status"] != "routed":
                raise LiveRoutingError()
            return decision
        except Exception:
            self.ledger.finish(operation, {"status": "live_routing_unavailable"},
                               billed=billed, halt="live_routing_unavailable")
            raise LiveRoutingError() from None


class BoundHostRouting:
    """Trusted per-session discovery and optional host-owned dispatch boundary.

    The host dispatcher must enforce generation budgets, permissions and durable
    idempotency itself. It receives validated controls and an immutable receipt;
    no arbitrary caller-supplied receipt can enter this execution path.
    """
    def __init__(self, *, runtime_id, session_id, discover, selector, authorize, dispatch=None, clock=time.time):
        self.runtime_id, self.session_id = runtime_id, session_id
        self.discover, self.selector, self.authorize = discover, selector, authorize
        self.dispatch, self.clock = dispatch, clock
        self._lock = Lock()
        self._attempted = set()

    def recommend(self, task):
        task = TaskEnvelope.model_validate(task)
        if self.authorize(task) is not True:
            raise LiveRoutingError()
        snapshot = CapabilitySnapshot.model_validate(self.discover())
        if snapshot.evidence_status != "runtime_observed":
            raise LiveRoutingError()
        return recommend_route(task, snapshot, runtime_id=self.runtime_id, session_id=self.session_id,
                               selector=self.selector, now=self.clock())

    def run(self, task):
        if self.dispatch is None:
            raise LiveRoutingError()
        task = TaskEnvelope.model_validate(task)
        with self._lock:
            if task.request_id in self._attempted:
                raise LiveRoutingError()
            self._attempted.add(task.request_id)
        receipt = self.recommend(task)
        if (receipt.status != "recommended" or receipt.routing_evidence_status != "jev_observed"
                or receipt.routing_cost_kind != "billed" or self.authorize(task) is not True
                or self.clock() >= task.deadline):
            raise LiveRoutingError()
        # Revalidate discovery immediately before dispatch; a changed observation
        # needs a new JEV decision, never a silent substitution.
        snapshot = CapabilitySnapshot.model_validate(self.discover())
        from routing_policy import POLICY_VERSION
        canonical = {"policy_version": POLICY_VERSION, "task": task.to_dict(), "snapshot": snapshot.to_dict()}
        fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                                allow_nan=False).encode()).hexdigest()
        if fingerprint != receipt.routing_request_fingerprint or self.clock() >= snapshot.expires_at:
            raise LiveRoutingError()
        return self.dispatch(task=task, decision=receipt)


def create_routing_server(host_routing: BoundHostRouting):
    """Build a separate stdio MCP server without importing legacy key loaders.

    Host setup must authenticate/authorize each principal before constructing a
    per-session binding. Do not expose this stdio server as anonymous HTTP.
    """
    if type(host_routing) is not BoundHostRouting:
        raise LiveRoutingError()
    from safe_mcp import SafeMCPServer
    from portability_tools import register_portability_tools
    server = SafeMCPServer("jev-bound-routing", instructions=
        "Recommend with the bound runtime catalog and JEV. Recommendations do not grant permission.")
    register_portability_tools(server, host_routing=host_routing)
    return server
