"""Shared mock-only MCP interfaces, identical on the local and remote servers.

No request field conveys caller identity. The hosting transport owns the trusted
access context, and the writing service independently authenticates and scopes it.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_access_token: ContextVar[str | None] = ContextVar("jev_writing_access", default=None)


@contextmanager
def trusted_access(token: str | None):
    """Transport/test harness boundary only; never expose this as an MCP tool."""
    marker = _access_token.set(token)
    try:
        yield
    finally:
        _access_token.reset(marker)


def register_portability_tools(mcp):
    from writing_service import PlanRequest, GenerationRequest
    import writing_service
    from host_contracts import CapabilitySnapshot, TaskEnvelope
    from host_adapters import recommend_route

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                          "idempotentHint": True, "openWorldHint": False})
    def jev_plan_writing(request: PlanRequest) -> dict:
        """Plan latest-family Anthropic writing. Mock-only; no generation or live calls."""
        return writing_service.plan_writing(request.model_dump(),
                                            access_token=_access_token.get())

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                          "idempotentHint": True, "openWorldHint": False})
    def jev_generate_writing(request: GenerationRequest) -> dict:
        """Return a mock Anthropic draft unchanged plus receipt; live generation disabled."""
        return writing_service.generate_writing(request.model_dump(),
                                                access_token=_access_token.get())

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                          "idempotentHint": True, "openWorldHint": False})
    def jev_recommend_host_route(task: TaskEnvelope, snapshot: CapabilitySnapshot,
                                 runtime_id: str, session_id: str) -> dict:
        """Recommend from an observed host catalog only. Never dispatch or grant permission."""
        # Tool input is an untrusted observation. Only an independently bound
        # host adapter can attest actual runtime discovery; this endpoint cannot.
        snapshot = snapshot.model_copy(update={"evidence_status": "synthetic"})
        result = recommend_route(task, snapshot, runtime_id=runtime_id, session_id=session_id)
        return result.model_dump(mode="json")
