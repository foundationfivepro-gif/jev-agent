"""Real offline transport/schema checks; these do not certify connected hosts."""
import asyncio
import io
import json
import urllib.error
import hashlib
from pathlib import Path
import time

import pytest

import core
from privacy import PrivacyError


def test_U03_canonical_command_source_validates_both_servers():
    import mcp_server
    import remote_server
    for module in (mcp_server, remote_server):
        item = module.CommandDecision(decision="allow", reason="fixture", binaries=["echo"],
                                      source="operator_allowlist")
        assert item.source == "operator_allowlist"
        schema = module.CommandDecision.model_json_schema()
        assert schema["properties"]["source"]["enum"] == [
            "policy", "operator_allowlist", "model", "unavailable"]


def test_S03_full_transport_screen_before_request_and_trace(monkeypatch, tmp_path):
    canary = "Authorization: Bearer synthetic-private-token-123456"
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-upstream")
    seen = []
    monkeypatch.setattr(core, "_open_decision_request", lambda *a, **k: seen.append(a))
    with pytest.raises(PrivacyError) as exc:
        core.decide({"nested": {"metadata": canary}}, {"safe": core.Noul(instructions="Is this safe?")})
    assert seen == []
    assert canary not in str(exc.value)
    monkeypatch.setattr(core, "TRACE_DIR", tmp_path)
    path = core.write_trace("fixture", {"text": canary}, {"why": canary}, meta={"detail": canary})
    assert canary not in path.read_text()
    assert "[redacted]" in path.read_text()


def test_S11_transport_endpoint_and_error_do_not_leak(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-upstream")
    monkeypatch.setattr(core, "TRANSPORTS", {
        "openrouter": ("OPENROUTER_API_KEY", "https://example.invalid/?token=dummy", "jev-latest", "noul")})
    with pytest.raises(core.TransportError, match="unsafe upstream") as exc:
        core.decide({}, {"safe": core.Noul(instructions="Is this safe?")})
    assert "token=dummy" not in str(exc.value)


def test_F01_upstream_error_body_is_not_exposed(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-upstream")
    secret = "Authorization: Bearer synthetic-echoed-token-123456"
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("https://example.invalid", 401, "failed", {},
                                     io.BytesIO(secret.encode()))
    monkeypatch.setattr(core, "_open_decision_request", fail)
    with pytest.raises(core.TransportError) as exc:
        core.decide({}, {"safe": core.Noul(instructions="Is this safe?")})
    assert secret not in str(exc.value)
    assert exc.value.__suppress_context__


def test_I01_S11_no_authenticated_decision_redirects(monkeypatch):
    request = core.urllib.request.Request("https://openrouter.ai/api/v1/systemone",
                                         data=b"{}", headers={"Authorization": "Bearer dummy-upstream"})
    handler = core._NoDecisionRedirects()
    for code in (301, 302, 303, 307, 308):
        assert handler.redirect_request(request, None, code, "redirect", {},
                                        "https://other-provider.invalid/collect") is None
    seen = []
    class Opener:
        def open(self, request, timeout):
            seen.append((request.full_url, timeout))
            return "synthetic-response"
    def build(handler):
        assert isinstance(handler, core._NoDecisionRedirects)
        return Opener()
    monkeypatch.setattr(core.urllib.request, "build_opener", build)
    assert core._open_decision_request(request, timeout=3) == "synthetic-response"
    assert seen == [("https://openrouter.ai/api/v1/systemone", 3)]


@pytest.mark.parametrize("module_name", ["mcp_server", "remote_server"])
def test_S03_S11_sdk_validation_never_echoes_invalid_values(module_name, caplog):
    import importlib
    from mcp.server.mcpserver.exceptions import ToolError
    module = importlib.import_module(module_name)
    canary = "api_key=syntheticVALUE0123456789"
    async def check():
        requests = [
            ("jev_plan_writing", {"request": {"request_id": canary}}),
            ("jev_generate_writing", {"request": {canary: "private"}}),
            ("jev_evaluate", {"state": {}, "questions": [{"id": "q", "private": canary}]}),
        ]
        for name, args in requests:
            with pytest.raises(ToolError) as exc:
                await module.mcp.call_tool(name, args)
            assert canary not in str(exc.value)
            assert exc.value.__suppress_context__
            assert canary not in caplog.text
    asyncio.run(check())


def test_U04_empty_catalog_survives_public_local_wrapper(monkeypatch):
    import mcp_server
    monkeypatch.setattr(mcp_server, "_require_key", lambda: None)
    result = mcp_server.jev_route_model("Synthetic task", catalog={})
    assert result.selected == "human" and result.fallback == "no_eligible_model"


def test_R03_installation_and_deployment_files_unchanged():
    expected = {
        ".claude/settings.json": "c55eaf4f32a94721f416468b4996660476f44233",
        ".mcp.json": "938388167de942a051d87412965f9ec004659255",
        "install.sh": "f03c963ee6fe987f5307d26bb641ebf5cee8de1d",
        "update.sh": "98e085e9e428a8488168199029836b5700692a55",
        "vercel.json": "e3c8c7cf4906bf79fde73732c569804906127284",
        "api/index.py": "78ad9bdc2cf9b30190eb6bc2c77e23795f67b2b6",
    }
    root = Path(__file__).resolve().parent.parent
    for name, digest in expected.items():
        data = (root / name).read_bytes()
        assert hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == digest


@pytest.mark.parametrize("module_name", ["mcp_server", "remote_server"])
def test_host_tool_cannot_attest_its_own_observation(module_name):
    import importlib
    from test_host_adapters import snapshot, task
    module = importlib.import_module(module_name)
    now = time.time()
    async def check():
        result = await module.mcp.call_tool("jev_recommend_host_route", {
            "task": task(deadline=now + 100),
            "snapshot": snapshot(observed_at=now - 1, expires_at=now + 100,
                                 evidence_status="runtime_observed"),
            "runtime_id": "runtime-1", "session_id": "session-1",
        })
        value = json.loads(result.content[0].text)
        assert value["evidence_status"] == "synthetic"
        assert value["dispatch_enabled"] is False and value["permission_granted"] is False
    asyncio.run(check())


def test_I02_identical_tools_schemas_and_default_disabled():
    import mcp_server
    import remote_server
    async def check():
        surfaces = []
        for module in (mcp_server, remote_server):
            tools = {t.name: t for t in await module.mcp.list_tools()}
            assert {"jev_plan_writing", "jev_generate_writing", "jev_recommend_host_route"} <= tools.keys()
            assert "access_token" not in json.dumps(tools["jev_plan_writing"].input_schema)
            surfaces.append(tools)
            result = await module.mcp.call_tool("jev_generate_writing",
                                                {"request": {"plan_id": "missing", "request_id": "fixture"}})
            serialized = result.model_dump_json()
            assert "unavailable" in serialized
            assert "generated" not in serialized
        for name in ("jev_plan_writing", "jev_generate_writing", "jev_recommend_host_route"):
            assert surfaces[0][name].input_schema == surfaces[1][name].input_schema
    asyncio.run(check())


def test_S11_legacy_url_token_is_rejected(monkeypatch):
    import remote_server
    from starlette.requests import Request
    middleware = remote_server.PathToken(lambda *args: None)
    async def check():
        for path, query in (("/t/dummy-token/mcp", b""), ("/mcp", b"token=dummy")):
            request = Request({"type": "http", "path": path, "query_string": query,
                               "headers": [], "scheme": "https", "server": ("fixture.invalid", 443)})
            called = []
            async def downstream(request):
                called.append(True)
            result = await middleware.dispatch(request, downstream)
            assert result.status_code == 400 and not called
            assert b"dummy" not in result.body
    asyncio.run(check())


@pytest.mark.parametrize("module_name", ["mcp_server", "remote_server"])
def test_I02_I04_real_mcp_discovery_plan_generate_mock_transport(module_name, monkeypatch):
    """SDK discovery/calls are real in process; provider/auth evidence is synthetic."""
    import importlib
    import writing_service as writing
    from writing_auth import MockOAuthBoundary, Principal
    from portability_tools import trusted_access
    clock = lambda: 1000.0
    auth = MockOAuthBoundary(clock=clock)
    principal = Principal("tenant-a", "subject-a",
                          frozenset({"writing:plan", "writing:generate", "writing:read"}),
                          frozenset({("openrouter", "public"), ("fixture-provider", "public")}))
    token = auth.issue_mock_token(principal)
    provider = writing.Provider("fixture-provider", True, True, 900.0, 2000.0)
    target = writing.ModelTarget(writing.ALIASES["sonnet"], "anthropic/fixture-sonnet",
                                 "sonnet", True, True, 4096, 32000,
                                 writing.Prices("1", "2", "0", "0"), (provider,),
                                 "fixture-catalog-v1", 900.0, 2000.0)
    draft = "  A synthetic draft.\nKeep exact spacing.\n"
    transport = writing.MockWritingTransport([{
        "draft": draft, "actual_model": target.model_id, "provider": provider.provider_id,
        "usage": {"input_tokens": 10, "output_tokens": 10},
        "finish_reason": "stop", "billed_usd": "0.00003",
    }])
    service = writing.WritingService(catalog=writing.MockCatalog({target.alias: target}), auth=auth,
                                    transport=transport, ledger=writing.BudgetLedger({"tenant-a": "1"}),
                                    clock=clock, enabled=True, selector=writing.MockDecisionSelector("sonnet"))
    monkeypatch.setattr(writing, "DEFAULT_SERVICE", service)
    module = importlib.import_module(module_name)
    async def call(name, request):
        result = await module.mcp.call_tool(name, {"request": request})
        assert not result.is_error
        return json.loads(result.content[0].text)
    async def check():
        with trusted_access(token):
            plan = await call("jev_plan_writing", {
                "request_id": "plan-fixture", "brief": "Write a short public product note.",
                "allowed_destinations": ["openrouter", "fixture-provider"], "budget_usd": "1",
                "max_output_tokens": 100, "deadline": 1500.0,
            })
            assert plan["status"] == "planned"
            assert transport.calls == []
            request = {"request_id": "generation-fixture", "plan_id": plan["plan"]["plan_id"]}
            result = await call("jev_generate_writing", request)
            assert result["status"] == "generated"
            assert result["draft"] == draft
            assert result["receipt"]["actual_model"] == target.model_id
            assert len(transport.calls) == 1
            replay = await call("jev_generate_writing", request)
            assert replay["status"] == "already_completed" and len(transport.calls) == 1
        assert (await call("jev_generate_writing", request))["status"] == "unauthenticated"
        # E08 offline rollback: disable new generation; privacy remains active.
        service.enabled = False
        with trusted_access(token):
            assert (await call("jev_generate_writing", request))["status"] == "unavailable"
        assert len(transport.calls) == 1
        with pytest.raises(PrivacyError):
            core.screen_outbound("Authorization: Bearer synthetic-private-token-123456")
    asyncio.run(check())
