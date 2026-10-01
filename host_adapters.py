"""Host-aware recommendations require an independently bound JEV selector.

Live providers are supplied only through the guarded composition root; there is
no deterministic model fallback. Namespace labels never convert API IDs to host executor controls.
Snapshots and decision IDs are observations, not authentication or permission.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import time
from typing import Any, Literal, Mapping, Protocol

from pydantic import TypeAdapter, ValidationError

from host_contracts import (CapabilitySnapshot, DecisionResponse, Identifier, JevRouteDecision,
                            ModelCapability, Mode, TaskEnvelope)
from routing_policy import POLICY_VERSION, RoutingPolicyError, jev_selection_scope
from privacy import PrivacyError, safe_metadata, screen_outbound

COMPATIBILITY_IDS = frozenset({"gpt-6-sol", "gpt-5.6-sol"})
NATIVE_FAMILIES = {"gpt-6-luna": "luna", "gpt-6.1-sol": "sol", "gpt-6-astra": "astra",
                   "gpt-6-sol": "sol", "gpt-5.6-sol": "sol"}
SURFACE_NAMESPACES = {
    "claude": {"claude_executor"},
    "codex_local": {"openai_native"},
    "codex_delegated": {"openai_native"},
    "dot_parent": {"openai_native"},
    "dot_executor": {"openai_native"},
    "chatgpt_cloud": {"chatgpt_ui"},
}


class JevSelectorCallback(Protocol):
    def __call__(self, *, task: TaskEnvelope, runtime_id: str, session_id: str,
                 request_fingerprint: str, eligible_models: tuple[ModelCapability, ...],
                 execution_modes: tuple[Mode, ...]) -> JevRouteDecision | Mapping[str, Any] | None:
        ...


@dataclass(frozen=True)
class TrustedJevSelector:
    """In-process composition-root binding, never accepted as tool input.

    Only trusted server setup may install this callback and attest its source.
    A decision candidate cannot supply or upgrade these provenance fields. The
    binding itself does not authorize a paid call, data transmission or dispatch;
    any live integration must independently enforce those requirements and
    reserve the routing budget before a call. Live bindings require the guarded
    OpenRouterSelector. MCP activation requires explicit trusted host setup.
    """
    source_id: str
    evidence_status: Literal["synthetic", "jev_observed"]
    select: JevSelectorCallback

    def __post_init__(self):
        TypeAdapter(Identifier).validate_python(self.source_id, strict=True)
        if self.evidence_status not in {"synthetic", "jev_observed"}:
            raise ValueError("JEV binding requires server-controlled provenance")
        if not callable(self.select):
            raise TypeError("JEV binding requires a selector callback")


def _parse(contract, value):
    # Reparse dumps as well: frozen models can still contain mutable nested lists.
    return contract.model_validate(value.model_dump() if isinstance(value, contract) else value)


def _failure(request_id, status, explanation, *, evidence="unverified"):
    if safe_metadata(request_id) == "[redacted]":
        request_id = "redacted-request"
    return DecisionResponse(request_id=request_id, status=status, failure=status,
                            explanation=explanation, evidence_status=evidence,
                            source="unavailable" if status == "unavailable" else "policy")


def _host_candidates(task: TaskEnvelope, snapshot: CapabilitySnapshot) -> list[ModelCapability]:
    tools = {tool.name for tool in snapshot.observed_tools}
    if not set(task.required_tools).issubset(tools):
        return []
    return [model for model in snapshot.models if model.available
            and model.namespace in SURFACE_NAMESPACES[snapshot.surface]
            and set(task.required_tools).issubset(model.supported_tools)
            and (task.explicit_effort is None or task.explicit_effort in model.efforts)]


def _override_allowed(model, effort, mode, snapshot):
    # A native delegate is an independent task, but model/effort parameters still
    # require observed host controls. No permission is inferred from delegation.
    same = (model.model_id == snapshot.current_model_id and model.namespace == snapshot.current_model_namespace)
    if not same and "model_override" not in snapshot.dispatch_controls:
        return False
    if effort is not None and effort != snapshot.current_effort and "effort_override" not in snapshot.dispatch_controls:
        return False
    return mode in snapshot.dispatch_controls


def recommend_route(task: TaskEnvelope | Mapping[str, Any], snapshot: CapabilitySnapshot | Mapping[str, Any],
                    *, runtime_id: str, session_id: str, now: float | None = None,
                    selector: TrustedJevSelector | None = None) -> DecisionResponse:
    """Preflight, call one bound JEV selector, validate, and record its decision.

    Even an explicit user model choice requires a recorded JEV decision. JEV is
    given only eligible choices and cannot override user or host constraints.
    Omitting the binding fails closed; no policy fallback or live call is built
    in. Runtime/session identity and the selector must be bound independently of
    caller-supplied observations. This function never dispatches anything.
    """
    try:
        task = _parse(TaskEnvelope, task)
    except (ValidationError, TypeError, ValueError):
        return _failure("invalid_request", "invalid_request", "Task envelope failed strict schema validation.")
    try:
        snapshot = _parse(CapabilitySnapshot, snapshot)
    except (ValidationError, TypeError, ValueError):
        return _failure(task.request_id, "invalid_catalog", "Capability snapshot failed strict schema validation.")
    fail = lambda status, explanation: _failure(task.request_id, status, explanation, evidence=snapshot.evidence_status)
    instant = time.time() if now is None else now
    try:
        valid_clock = not isinstance(instant, bool) and isinstance(instant, (int, float)) and math.isfinite(instant)
    except OverflowError:
        valid_clock = False
    if not valid_clock:
        return fail("invalid_request", "Routing requires a finite clock value.")
    if runtime_id != snapshot.runtime_id or session_id != snapshot.session_id:
        return fail("unavailable", "This runtime and session need their own capability discovery.")
    if not snapshot.available or not snapshot.observed_at <= instant < snapshot.expires_at:
        return fail("unavailable", "Capability observation is unavailable, future-dated, or expired.")
    if snapshot.evidence_status == "unverified":
        return fail("unavailable", "Capabilities have not been observed or verified with a synthetic fixture.")
    if task.deadline <= instant:
        return fail("unavailable", "The task deadline has expired.")
    if any(m.namespace == "openai_native" and m.model_id in NATIVE_FAMILIES
           and m.family != NATIVE_FAMILIES[m.model_id] for m in snapshot.models):
        return fail("invalid_catalog", "Native family metadata conflicts with the exact observed identifier.")
    eligible = _host_candidates(task, snapshot)
    if not eligible:
        return fail("no_eligible_model", "No observed executable model meets the requested tools and effort.")
    # Explicit user identity narrows the set before the JEV selection call.
    if task.explicit_model_id is not None:
        eligible = [m for m in eligible if (m.model_id, m.namespace) ==
                    (task.explicit_model_id, task.explicit_model_namespace)]
    if task.explicit_model_family is not None:
        eligible = [m for m in eligible if m.family == task.explicit_model_family]
    if not eligible:
        return fail("choice_conflict", "The explicit model or family choice is not available on this host.")
    explicit = task.explicit_model_id is not None or task.explicit_model_family is not None
    if not explicit:
        eligible = [m for m in eligible if m.model_id not in COMPATIBILITY_IDS or
                    m.model_id == task.compatibility_model_id]
    if not eligible:
        return fail("no_eligible_model", "Older compatibility models require an explicit choice or compatibility evidence.")
    modes = tuple(mode for mode in ("inline", "delegate") if mode in snapshot.dispatch_controls)
    if not modes:
        return fail("unavailable", "This host exposes no supported execution mode.")
    if "model_override" not in snapshot.dispatch_controls:
        eligible = [m for m in eligible if (m.model_id, m.namespace) ==
                    (snapshot.current_model_id, snapshot.current_model_namespace)]
        if not eligible:
            return fail("choice_conflict" if explicit else "unavailable",
                        "This host cannot switch to the requested model.")
    if "effort_override" not in snapshot.dispatch_controls:
        if task.explicit_effort is not None and task.explicit_effort != snapshot.current_effort:
            return fail("choice_conflict", "This host cannot change to the requested effort.")
        eligible = [m.model_copy(update={"efforts": [e for e in m.efforts if e == snapshot.current_effort]})
                    for m in eligible]
    if task.explicit_effort is not None:
        eligible = [m.model_copy(update={"efforts": [task.explicit_effort]}) for m in eligible]
    # JSON-looking metadata or a caller-supplied candidate cannot install a
    # server-side selector. No public MCP argument is accepted at this boundary.
    if not isinstance(selector, TrustedJevSelector):
        return fail("unavailable", "A trusted JEV selector is required; no model recommendation was made.")
    if selector.evidence_status == "jev_observed":
        from live_routing import OpenRouterSelector
        if type(selector.select) is not OpenRouterSelector:
            return fail("unavailable", "Live selector requires the guarded OpenRouter composition root.")
    # Screen the original inputs, including unused tool-schema text, before the
    # extension boundary. No prose claiming a local exception can exempt a model
    # call. Even screened inputs are minimized before they reach the callback.
    if task.data_class in {"confidential", "sensitive", "secret"}:
        return fail("privacy_unavailable", "This routing boundary does not accept sensitive task content.")
    try:
        canonical = {"policy_version": POLICY_VERSION, "task": task.to_dict(), "snapshot": snapshot.to_dict()}
        screen_outbound(canonical)
        screen_outbound(selector.source_id)
        fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                                allow_nan=False).encode()).hexdigest()
    except (PrivacyError, ValueError, TypeError, RecursionError):
        return fail("privacy_unavailable", "Routing inputs failed the local privacy boundary.")
    try:
        selection_started = time.monotonic()
        with jev_selection_scope():
            response = selector.select(
                task=task.model_copy(update={"context_references": []}, deep=True),
                runtime_id=runtime_id, session_id=session_id, request_fingerprint=fingerprint,
                eligible_models=tuple(m.model_copy(deep=True) for m in eligible), execution_modes=modes)
        elapsed = time.monotonic() - selection_started
        if (not math.isfinite(elapsed) or elapsed < 0 or
                instant + elapsed >= min(task.deadline, snapshot.expires_at)):
            return fail("unavailable", "JEV selection outlived the task deadline or capability observation.")
    except RoutingPolicyError:
        return fail("unavailable", "Recursive JEV selection is blocked; no model recommendation was made.")
    except Exception:
        # Provider errors must not leak task content or credentials and must not
        # trigger an unrecorded fallback or a second selection attempt.
        return fail("unavailable", "The bound JEV selector is unavailable; no model recommendation was made.")
    if response is None:
        return fail("unavailable", "The bound JEV selector returned no decision.")
    try:
        decision = _parse(JevRouteDecision, response)
    except (ValidationError, TypeError, ValueError):
        return fail("invalid_response", "JEV decision failed strict schema validation.")
    if ((decision.request_id, decision.runtime_id, decision.session_id, decision.request_fingerprint) !=
            (task.request_id, runtime_id, session_id, fingerprint)):
        return fail("invalid_response", "JEV decision does not match this exact task and host observation.")
    try:
        screen_outbound(decision.to_dict())
    except PrivacyError:
        return fail("invalid_response", "JEV decision failed the local privacy boundary.")
    if task.budget_usd is not None and decision.cost_usd is not None and decision.cost_usd > task.budget_usd:
        return fail("budget_exceeded", "Recorded routing cost exceeds this task's budget.")
    proposal = decision.candidate
    model = next((m for m in eligible if (m.model_id, m.namespace) ==
                  (proposal.selected_model_id, proposal.selected_model_namespace)), None)
    if model is None or (proposal.selected_effort is not None and proposal.selected_effort not in model.efforts):
        return fail("invalid_response", "JEV selected an ineligible model or unsupported effort.")
    if task.explicit_effort is not None and proposal.selected_effort != task.explicit_effort:
        return fail("invalid_response", "JEV did not preserve the explicit effort.")
    mode, effort = proposal.execution_mode, proposal.selected_effort
    if not _override_allowed(model, effort, mode, snapshot):
        return fail("invalid_response", "JEV requested model, effort, or dispatch controls this host does not expose.")
    return DecisionResponse(request_id=task.request_id, status="recommended", selected_model_id=model.model_id,
                            selected_model_namespace=model.namespace, selected_effort=effort,
                            execution_mode=mode,
                            explanation="Validated JEV recommendation; host authorization is still required.",
                            evidence_status=snapshot.evidence_status, source="jev",
                            routing_decision_id=decision.decision_id, routing_request_fingerprint=fingerprint,
                            routing_source_id=selector.source_id, routing_evidence_status=selector.evidence_status,
                            routing_usage=decision.usage, routing_cost_usd=decision.cost_usd,
                            routing_cost_kind=("billed" if selector.evidence_status == "jev_observed" else "synthetic")
                            if decision.cost_usd is not None else "unknown")


def dispatch_recommendation(decision: DecisionResponse | Mapping[str, Any], **_unused) -> DecisionResponse:
    """No live dispatcher is included in this approved mock-only implementation."""
    try:
        decision = _parse(DecisionResponse, decision)
    except (ValidationError, TypeError, ValueError):
        return _failure("invalid_request", "invalid_response", "Decision response failed strict schema validation.")
    return _failure(decision.request_id, "unavailable", "Live dispatch is disabled; obtain independent host authorization.")


class HostAdapter:
    """Bind trusted runtime identity, surface, and JEV selector at server setup."""
    surfaces: frozenset[str] = frozenset()

    def __init__(self, *, runtime_id: str, session_id: str, selector: TrustedJevSelector | None = None):
        self.runtime_id, self.session_id, self.selector = runtime_id, session_id, selector

    def recommend(self, task, snapshot, *, now: float | None = None) -> DecisionResponse:
        try:
            parsed = _parse(CapabilitySnapshot, snapshot)
        except (ValidationError, TypeError, ValueError):
            return recommend_route(task, snapshot, runtime_id=self.runtime_id, session_id=self.session_id,
                                   selector=self.selector, now=now)
        if parsed.surface not in self.surfaces:
            return _failure("invalid_request", "unavailable", "Snapshot belongs to a different host surface.")
        return recommend_route(task, parsed, runtime_id=self.runtime_id, session_id=self.session_id,
                               selector=self.selector, now=now)


class ClaudeAdapter(HostAdapter):
    surfaces = frozenset({"claude"})


class LocalCodexAdapter(HostAdapter):
    surfaces = frozenset({"codex_local", "codex_delegated"})


class ChatGPTAdapter(HostAdapter):
    surfaces = frozenset({"chatgpt_cloud"})


class DotAdapter(HostAdapter):
    surfaces = frozenset({"dot_parent", "dot_executor"})
