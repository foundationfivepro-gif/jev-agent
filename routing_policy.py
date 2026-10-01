"""Mandatory JEV-selection invariant and narrow non-model local exception.

These records never authorize execution. Host permissions and sandboxing remain
authoritative. The internal JEV control call is not recursively routed through
JEV; a selector trying to re-enter selection fails closed.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import re
import logging


POLICY_VERSION = "jev-required-2026-10-01"
_selecting = ContextVar("jev_selection_in_progress", default=False)
LOCAL_OPERATIONS = frozenset({"local_file_io", "local_test", "local_build",
                              "local_environment_inspection"})


class RoutingPolicyError(ValueError):
    def __init__(self, code="jev_routing_required"):
        self.code = code if code in {"jev_routing_required", "recursive_routing_blocked",
                                    "invalid_local_exception"} else "jev_routing_required"
        super().__init__(self.code)


@contextmanager
def jev_selection_scope():
    """Wrap the one internal selector call; never wrap it in another router."""
    if _selecting.get():
        raise RoutingPolicyError("recursive_routing_blocked")
    marker = _selecting.set(True)
    try:
        yield
    finally:
        _selecting.reset(marker)


def record_local_exception(request_id: str, operation: str, reason: str, *,
                           is_model_execution: bool = False) -> dict:
    """Record a named non-model local requirement; this is NOT a dispatch API.

    This receipt is never accepted in place of a JEV decision by model adapters.
    Even local model inference counts as model execution and cannot use it.
    """
    if (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id)
            or operation not in LOCAL_OPERATIONS or is_model_execution is not False
            or not isinstance(reason, str) or not reason.strip()):
        raise RoutingPolicyError("invalid_local_exception")
    from privacy import safe_metadata
    safe_id, safe_reason = safe_metadata(request_id), safe_metadata(reason)
    if safe_id == "[redacted]":
        raise RoutingPolicyError("invalid_local_exception")
    record = {"policy_version": POLICY_VERSION, "request_id": safe_id,
            "status": "direct_local_exception", "operation": operation,
            "reason": safe_reason, "uses_model": False, "permission_granted": False}
    # Emit only the already sanitized fixed-shape receipt; never the input text.
    logging.getLogger(__name__).info("direct_local_exception %s", record)
    return record
