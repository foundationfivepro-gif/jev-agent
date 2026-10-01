"""Versioned, strict host-routing contracts. These describe observations, not grants.

Every surface, including a delegated executor, supplies its own current snapshot.
No model identifier is translated between UI, native, API, or OpenRouter spaces.
"""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from routing_policy import POLICY_VERSION

SCHEMA_VERSION = "1.0"
Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/~-]*$")]
Text = Annotated[str, Field(min_length=1, max_length=4000)]
Number = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "persistent"]
Namespace = Literal["claude_executor", "openai_native", "openai_api", "openrouter", "chatgpt_ui"]
Surface = Literal["claude", "codex_local", "codex_delegated", "chatgpt_cloud", "dot_parent", "dot_executor"]
Mode = Literal["inline", "delegate"]
Failure = Literal["unavailable", "invalid_catalog", "invalid_request", "invalid_response", "no_eligible_model",
                  "choice_conflict", "approval_required", "privacy_unavailable", "budget_exceeded"]


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, revalidate_instances="always")

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")


class ToolCapability(StrictContract):
    name: Identifier
    input_schema: dict[str, Any]

    @field_validator("input_schema")
    @classmethod
    def bounded_json_schema(cls, value: dict) -> dict:
        # Observation must contain the actual schema, not only a tool label.
        if value.get("type") != "object":
            raise ValueError("observed tool schema must describe an object")
        try:
            encoded = json.dumps(value, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError("tool schema must be finite JSON") from exc
        if len(encoded) > 65536:
            raise ValueError("tool schema is too large")
        return value


class ModelCapability(StrictContract):
    model_id: Identifier
    namespace: Namespace
    family: Identifier
    efforts: Annotated[list[Effort], Field(max_length=9)]
    supported_tools: Annotated[list[Identifier], Field(max_length=128)]
    available: bool
    cost_in: Number | None = None
    cost_out: Number | None = None

    @model_validator(mode="after")
    def unique_controls(self):
        if len(set(self.efforts)) != len(self.efforts) or len(set(self.supported_tools)) != len(self.supported_tools):
            raise ValueError("model controls must be unique")
        # A slash-qualified provider ID must never be interpreted as a native ID.
        if self.namespace in {"openai_native", "openai_api", "claude_executor"} and "/" in self.model_id:
            raise ValueError("provider-qualified ID is not a native executable ID")
        if self.namespace == "openrouter" and "/" not in self.model_id:
            raise ValueError("OpenRouter IDs must be provider-qualified")
        return self


class CapabilitySnapshot(StrictContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    surface: Surface
    runtime_id: Identifier
    session_id: Identifier
    observed_at: Number
    expires_at: Number
    available: bool
    observed_tools: Annotated[list[ToolCapability], Field(max_length=128)]
    dispatch_controls: Annotated[list[Literal["inline", "delegate", "model_override", "effort_override"]], Field(max_length=4)]
    models: Annotated[list[ModelCapability], Field(max_length=64)]
    current_model_id: Identifier | None = None
    current_model_namespace: Namespace | None = None
    current_effort: Effort | None = None
    evidence_status: Literal["synthetic", "runtime_observed", "unverified"]

    @model_validator(mode="after")
    def coherent_observation(self):
        if self.expires_at <= self.observed_at:
            raise ValueError("snapshot expiry must follow observation")
        pairs = [(m.namespace, m.model_id) for m in self.models]
        if len(set(pairs)) != len(pairs):
            raise ValueError("duplicate executable model")
        tools = [tool.name for tool in self.observed_tools]
        if len(set(tools)) != len(tools) or len(set(self.dispatch_controls)) != len(self.dispatch_controls):
            raise ValueError("duplicate observed tool or control")
        if (self.current_model_id is None) != (self.current_model_namespace is None):
            raise ValueError("current model must include its namespace")
        if self.current_model_id is not None:
            pair = (self.current_model_namespace, self.current_model_id)
            if pair not in pairs:
                raise ValueError("current model must appear in this snapshot")
            current = self.models[pairs.index(pair)]
            if self.current_effort is not None and self.current_effort not in current.efforts:
                raise ValueError("current effort is unsupported")
        elif self.current_effort is not None:
            raise ValueError("current effort needs an observed current model")
        return self


class TaskEnvelope(StrictContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    request_id: Identifier
    purpose: Text
    acceptance_criteria: Annotated[list[Text], Field(min_length=1, max_length=32)]
    explicit_model_id: Identifier | None = None
    explicit_model_namespace: Namespace | None = None
    explicit_model_family: Identifier | None = None
    explicit_effort: Effort | None = None
    context_references: Annotated[list[Identifier], Field(max_length=128)]
    data_class: Literal["public", "internal", "confidential", "sensitive", "secret"]
    authorized_destinations: Annotated[list[Identifier], Field(max_length=32)]
    budget_usd: Number | None
    deadline: Number
    task_kind: Literal["narrow", "substantial", "hard"]
    required_tools: Annotated[list[Identifier], Field(max_length=128)] = Field(default_factory=list)
    parent_has_context: bool = True
    inspection_larger_than_result: bool = False
    failed_acceptance_checks: Annotated[list[Text], Field(max_length=32)] = Field(default_factory=list)
    compatibility_model_id: Identifier | None = None
    compatibility_reason: Text | None = None

    @model_validator(mode="after")
    def complete_explicit_choices(self):
        if (self.explicit_model_id is None) != (self.explicit_model_namespace is None):
            raise ValueError("explicit model choice requires an exact namespace")
        if (self.compatibility_model_id is None) != (self.compatibility_reason is None):
            raise ValueError("compatibility choice requires evidence")
        return self


class RouteCandidate(StrictContract):
    """Untrusted JEV output: controls only, never authority or provenance."""
    selected_model_id: Identifier
    selected_model_namespace: Namespace
    selected_effort: Effort | None
    execution_mode: Mode


class RoutingUsage(StrictContract):
    """Known routing usage only. Unknown usage is represented by None."""
    input_tokens: Annotated[int, Field(ge=0, le=1_000_000_000)]
    output_tokens: Annotated[int, Field(ge=0, le=1_000_000_000)]


class JevRouteDecision(StrictContract):
    """A bound selector's decision, correlated to this request and host session.

    Provenance is deliberately absent: only trusted server-side binding supplies
    it. These identifiers are correlation evidence, not permission to dispatch.
    """
    decision_id: Identifier
    request_id: Identifier
    runtime_id: Identifier
    session_id: Identifier
    request_fingerprint: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    candidate: RouteCandidate
    usage: RoutingUsage | None
    cost_usd: Number | None


class DecisionResponse(StrictContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    policy_version: Literal["jev-required-2026-10-01"] = POLICY_VERSION
    request_id: Identifier
    status: Literal["recommended", "unavailable", "invalid_catalog", "invalid_request", "invalid_response",
                    "no_eligible_model", "choice_conflict", "approval_required", "privacy_unavailable", "budget_exceeded"]
    selected_model_id: Identifier | None = None
    selected_model_namespace: Namespace | None = None
    selected_effort: Effort | None = None
    execution_mode: Literal["inline", "delegate", "unavailable"] = "unavailable"
    explanation: Text
    evidence_status: Literal["synthetic", "runtime_observed", "unverified"] = "unverified"
    source: Literal["policy", "jev", "unavailable"]
    routing_decision_id: Identifier | None = None
    routing_request_fingerprint: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    routing_usage: RoutingUsage | None = None
    routing_cost_usd: Number | None = None
    routing_cost_kind: Literal["synthetic", "unknown"] | None = None
    routing_source_id: Identifier | None = None
    routing_evidence_status: Literal["synthetic", "jev_observed"] | None = None
    failure: Failure | None = None
    permission_granted: Literal[False] = False
    dispatch_enabled: Literal[False] = False

    @field_validator("permission_granted", "dispatch_enabled", mode="before")
    @classmethod
    def exact_false(cls, value):
        # Python's 0 == False must not allow a malformed JSON number through a
        # security-significant literal-boolean contract.
        if value is not False:
            raise ValueError("recommendations cannot grant permission or enable dispatch")
        return value

    @model_validator(mode="after")
    def coherent_result(self):
        if self.status == "recommended":
            if self.failure is not None or self.selected_model_id is None or self.selected_model_namespace is None:
                raise ValueError("recommendation requires a choice and no failure")
            if self.execution_mode == "unavailable":
                raise ValueError("recommendation requires an execution mode")
            if (self.source != "jev" or self.routing_decision_id is None or
                    self.routing_source_id is None or self.routing_evidence_status is None or
                    self.routing_request_fingerprint is None or self.routing_cost_kind is None):
                raise ValueError("recommendation requires a recorded, bound JEV decision")
            if ((self.routing_cost_usd is None) != (self.routing_cost_kind == "unknown") or
                    (self.routing_cost_kind == "synthetic" and self.routing_evidence_status != "synthetic")):
                raise ValueError("routing accounting must distinguish synthetic and unknown cost")
        elif (self.failure != self.status or self.selected_model_id is not None or
              self.selected_model_namespace is not None or self.selected_effort is not None or
              self.execution_mode != "unavailable" or self.source == "jev" or
              self.routing_decision_id is not None or self.routing_source_id is not None or
              self.routing_evidence_status is not None or self.routing_request_fingerprint is not None or
              self.routing_usage is not None or self.routing_cost_usd is not None or self.routing_cost_kind is not None):
            raise ValueError("failure must not carry an executable recommendation or JEV route")
        return self
