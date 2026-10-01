import json
import pytest

from routing_policy import (LOCAL_OPERATIONS, RoutingPolicyError,
                            jev_selection_scope, record_local_exception)


def test_jev_selector_cannot_recursively_route_itself():
    with jev_selection_scope():
        with pytest.raises(RoutingPolicyError, match="recursive_routing_blocked"):
            with jev_selection_scope():
                pytest.fail("recursive selector executed")
    with jev_selection_scope():
        pass


@pytest.mark.parametrize("operation", sorted(LOCAL_OPERATIONS))
def test_named_local_exception_never_grants_permission(operation):
    record = record_local_exception("local-1", operation, "Requires local workspace access")
    assert record["uses_model"] is False and record["permission_granted"] is False
    assert record["operation"] == operation and record["reason"]


@pytest.mark.parametrize("operation,model", [("model_generation", False), ("local_test", True),
                                            ("local_model", True), ("anything", False)])
def test_model_cannot_claim_direct_local_exception(operation, model):
    with pytest.raises(RoutingPolicyError):
        record_local_exception("id", operation, "local", is_model_execution=model)


def test_local_exception_reason_is_sanitized_before_recording(caplog):
    caplog.set_level("INFO", logger="routing_policy")
    secret = "Authorization: Bearer synthetic-private-token-123456"
    record = record_local_exception("id", "local_test", secret)
    assert secret not in json.dumps(record)
    assert record["reason"] == "[redacted]"
    assert "direct_local_exception" in caplog.text and secret not in caplog.text


def test_codex_skill_states_enforceability_limits():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / ".agents/skills/codex-jev-routing/SKILL.md").read_text()
    for required in ["each new model execution", "explicit", "blocks", "recursively",
                     "platform-fixed", "does not install", "local model"]:
        assert required in text.lower()
