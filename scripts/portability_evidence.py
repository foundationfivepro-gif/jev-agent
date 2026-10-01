#!/usr/bin/env python3
"""Write reproducible local schema snapshots and 24 synthetic shadow records.

Run under the same cleared environment/socket guard as offline_tests.py.
This never calls a model, reads credentials or scores actual writing quality.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mcp_server
import remote_server
from writing_auth import MockOAuthBoundary, Principal
from writing_service import (
    ALIASES, BudgetLedger, MockCatalog, MockDecisionSelector, MockWritingTransport,
    ModelTarget, Prices, Provider, WritingService,
)


async def build(destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    inventories = {}
    for name, module in (("local", mcp_server), ("remote", remote_server)):
        tools = await module.mcp.list_tools()
        schemas = [tool.model_dump(mode="json", exclude_none=True) for tool in tools]
        encoded = json.dumps(schemas, indent=2, sort_keys=True)
        (destination / f"{name}-schemas.json").write_text(encoded)
        inventories[name] = {"count": len(tools), "tools": [tool.name for tool in tools],
                             "schema_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                             "evidence": "real SDK discovery, in-process VM only"}
    now = 1800000000.0
    clock = lambda: now
    auth = MockOAuthBoundary(clock=clock)
    provider = Provider("fixture-provider", True, True, now - 10, now + 3600)
    models = {
        alias: ModelTarget(alias, f"anthropic/fixture-{family}-stable", family, True, True,
                           4096, 32000, Prices("1", "2", "0", "0"), (provider,),
                           "synthetic-catalog-v1", now - 10, now + 3600)
        for family, alias in ALIASES.items()
    }
    principal = Principal("synthetic-tenant", "synthetic-reviewer",
                          frozenset({"writing:plan", "writing:generate", "writing:read"}),
                          frozenset({("openrouter", "public"), ("fixture-provider", "public")}))
    token = auth.issue_mock_token(principal)
    topics = ["release note", "public event invitation", "feature announcement", "landing-page copy",
              "newsletter summary", "product FAQ", "welcome message", "documentation introduction",
              "public update", "community announcement", "campaign copy", "editing brief"]
    responses = []
    for number in range(24):
        family = "sonnet" if number < 12 else "opus"
        responses.append({"draft": f"Synthetic unchanged fixture draft {number + 1}.",
                          "actual_model": models[ALIASES[family]].model_id,
                          "provider": provider.provider_id, "finish_reason": "stop",
                          "usage": {"input_tokens": 20, "output_tokens": 10},
                          "billed_usd": None})
    transport = MockWritingTransport(responses)
    selector = MockDecisionSelector()
    service = WritingService(catalog=MockCatalog(models), auth=auth, transport=transport,
                             ledger=BudgetLedger({"synthetic-tenant": "1"}), selector=selector,
                             clock=clock, enabled=True)
    records = []
    for number in range(24):
        complexity = "routine" if number < 12 else "complex"
        # Scripted JEV outcomes, not service-side complexity heuristics. Every
        # case must obtain its own injected decision before generation.
        selector.family = "sonnet" if number < 12 else "opus"
        request = {"request_id": f"shadow-plan-{number + 1}",
                   "brief": f"Write a {complexity} synthetic {topics[number % 12]} for a fictional public product.",
                   "complexity": complexity, "data_class": "public",
                   "allowed_destinations": ["openrouter", provider.provider_id], "budget_usd": "0.1",
                   "max_output_tokens": 100, "deadline": now + 600}
        plan = service.plan_writing(request, access_token=token)
        assert plan["status"] == "planned"
        result = service.generate_writing({"plan_id": plan["plan"]["plan_id"],
                                          "request_id": f"shadow-generation-{number + 1}"}, access_token=token)
        assert result["status"] == "generated"
        assert result["draft"] == f"Synthetic unchanged fixture draft {number + 1}."
        assert plan["plan"]["decision_calls"] == 1
        assert result["receipt"]["decision_id"] == plan["plan"]["decision_id"]
        assert result["receipt"]["decision_receipt"] == plan["plan"]["decision_receipt"]
        assert transport.calls[-1]["decision_id"] == plan["plan"]["decision_id"]
        records.append({"case": number + 1, "complexity": complexity, "topic": topics[number % 12],
                        "plan": plan["plan"], "receipt": result["receipt"], "draft_preserved": True,
                        "baseline": "not executed", "quality": "not measured",
                        "token_savings": "not measured"})
    assert selector.calls == 24
    assert len({record["receipt"]["decision_id"] for record in records}) == 24
    report = {"mode": "synthetic_offline", "generated_at": datetime.now(timezone.utc).isoformat(),
              "inventory": inventories, "shadow_cases": records,
              "mock_jev_decisions": selector.calls,
              "real_provider_calls": 0, "real_spend_usd": "0",
              "host_support": "unverified; no client registration or host execution",
              "savings_claim": None}
    (destination / "synthetic-shadow-24.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"inventories": inventories, "shadow_cases": len(records), "real_provider_calls": 0}))


if __name__ == "__main__":
    asyncio.run(build(Path(sys.argv[1])))
