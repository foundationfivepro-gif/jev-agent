"""Credential-free injected fixtures only; never implements an HTTP adapter."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from live_pilot import (ALIASES, BRIEFS, CAP_USD, ENDPOINT, JEV_ENDPOINT, JEV_MODEL, SURFACES, Approval,
                        Failure, LivePilot, PilotError, PilotLedger, Reply, RoutingPlan,
                        RoutingReceipt, RoutingRequest, Target, Usage)
from routing_policy import jev_selection_scope, record_local_exception


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class Catalog:
    def __init__(self, clock):
        self.clock = clock
        self.overrides = {}
        self.calls = 0

    def resolve(self, model, *, alias, prompt, max_output_tokens):
        self.calls += 1
        # Fixture-only bounded tokenizer, not a production tokenizer claim.
        target = Target(alias, model,
                        "anthropic", "fixture-1", self.clock() - 1, self.clock() + 100,
                        "0.000001", "0.000002", "0.0000001", "0.00000125", "0",
                        1024, 10000, 4096, True, True, True, True,
                        "sonnet" if "sonnet" in alias else "opus")
        return replace(target, **self.overrides)


class Router:
    """No-network JEV receipt fixture, never the production JEV SDK."""
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.next = []
        self.plan_overrides = {}
        self.receipt_overrides = {}

    def prepare(self, request, approval):
        return replace(RoutingPlan("fixture-routing-1", "0.01", 4096, 256,
                       self.clock() - 1, self.clock() + 100, "fixture"), **self.plan_overrides)

    def decide(self, **kwargs):
        self.calls.append(kwargs)
        if self.next:
            result = self.next.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        request = kwargs["request"]
        return replace(RoutingReceipt("fixture-decision-" + str(len(self.calls)), request.fingerprint(),
            "anthropic/mock-" + request.required_family, request.required_family,
            Usage(10, 4, 1, 0, 1), "0.0002", JEV_MODEL, "fixture"), **self.receipt_overrides)


class Transport:
    evidence_mode = "fixture"

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.next = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        if self.next:
            result = self.next.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        prompt = kwargs["payload"]["messages"][0]["content"]
        brief = BRIEFS[0] if "Lantern" in prompt else BRIEFS[1]
        return Reply("fixture-gen-" + str(len(self.calls)), kwargs["payload"]["model"], "anthropic",
                     Usage(len(prompt.split()), 30, 5, 2, 3), "0.001", "stop", " ".join(brief.required))


class Binding:
    def __init__(self, transport):
        self.transport = transport
        self.calls = 0

    def bind_preexisting(self, approval):
        self.calls += 1
        return self.transport


class LivePilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.approval = Approval("one-total-approved-pilot", "5", frozenset({"anthropic"}), 2000,
                                 jev_routing_approved=True)
        self.path = Path(self.tmp.name) / "ledger.sqlite"
        self.ledger = PilotLedger(self.path, self.approval)
        self.catalog = Catalog(self.clock)
        self.transport = Transport(self.clock)
        self.binding = Binding(self.transport)
        self.router = Router(self.clock)
        self.pilot = LivePilot(ledger=self.ledger, catalog=self.catalog, authorizer=lambda: self.approval,
                               secure_binding=self.binding, jev_router=self.router, clock=self.clock)
        socket_guard = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        socket_guard.start()
        self.addCleanup(socket_guard.stop)

    def run_one(self, surface=SURFACES[0], brief=BRIEFS[0].id, arm="baseline"):
        return self.pilot.run_one(surface, brief, arm)

    def reply(self, **overrides):
        return replace(Reply("fixture-123", "anthropic/mock-sonnet", "anthropic",
                             Usage(50, 20, 5, 2, 3), "0.001", "stop", "Lantern Tuesday offline search"), **overrides)

    def test_default_no_credential_or_transport_probe(self):
        self.assertEqual(LivePilot().run_predefined_pairs()["results"][0]["status"], "live_adapter_unprovisioned")
        self.pilot.authorizer = None
        self.assertEqual(self.run_one()["status"], "live_adapter_unprovisioned")
        self.assertEqual(self.binding.calls, 0)
        self.assertEqual(self.transport.calls, [])

    def test_jev_is_required_with_no_alias_or_local_exception_fallback(self):
        self.pilot.jev_router = None
        self.assertEqual(self.run_one()["status"], "jev_router_unprovisioned")
        self.assertEqual(self.catalog.calls, 0)
        self.assertEqual(self.binding.calls, 0)
        self.assertEqual(self.ledger.snapshot()["charged_usd"], "0")

    def test_routing_cost_reserved_before_call_and_same_ledger_settles_both(self):
        original = self.router.decide
        def checked(**kwargs):
            snapshot = self.ledger.snapshot()
            self.assertEqual(snapshot["reserved_usd"], "0.01")
            self.assertEqual(self.binding.calls, 0)
            self.assertEqual(kwargs["plan"].endpoint, JEV_ENDPOINT)
            self.assertEqual(kwargs["plan"].model, JEV_MODEL)
            self.assertEqual(kwargs["request"].max_output_tokens, 256)
            self.assertEqual(kwargs["request"].allowed_providers, ("anthropic",))
            return original(**kwargs)
        self.router.decide = checked
        result = self.run_one()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["decision"]["selected_model"], result["actual_model"])
        self.assertEqual(result["decision"]["usage"]["input_tokens"], 10)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot["charged_usd"], "0.0012")
        self.assertEqual(snapshot["reserved_usd"], "0")
        self.assertEqual(len(snapshot["receipts"]), 2)

    def test_invalid_or_unbounded_routing_preflight_never_calls(self):
        for override in ({"max_cost_usd": None}, {"max_cost_usd": "NaN"}, {"max_cost_usd": "6"},
                         {"max_cost_usd": Decimal("0.01")},
                         {"model": "other"}, {"endpoint": ENDPOINT}, {"expires_at": 999},
                         {"observed_at": 500}, {"input_token_upper_bound": 0},
                         {"max_output_tokens": 513}, {"evidence_mode": "unknown"},
                         {"evidence_mode": "live", "max_cost_usd": "0"}):
            with self.subTest(override=override):
                self.router.plan_overrides = override
                self.assertNotEqual(self.run_one()["status"], "completed")
        self.assertEqual(self.router.calls, [])
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.ledger.snapshot()["reserved_usd"], "0")

    def test_routing_outage_holds_charge_and_stops_all(self):
        self.router.next = [RuntimeError("CANARY_SECRET_provider_exception")]
        self.assertEqual(self.run_one()["status"], "uncertain_routing_charge")
        self.assertEqual(self.run_one(SURFACES[1])["status"], "uncertain_routing_charge")
        self.assertEqual(len(self.router.calls), 1)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.ledger.snapshot()["reserved_usd"], "0.01")
        self.assertNotIn("CANARY_SECRET", json.dumps(self.ledger.snapshot()))

    def test_routing_missing_cost_is_unknown_not_zero(self):
        self.router.receipt_overrides = {"billed_usd": None}
        self.assertEqual(self.run_one()["status"], "uncertain_routing_charge")
        self.assertEqual(self.ledger.snapshot()["reserved_usd"], "0.01")
        self.assertEqual(self.transport.calls, [])

    def test_decimal_billing_is_serialized_without_losing_charge(self):
        self.router.receipt_overrides = {"billed_usd": Decimal("0.0002")}
        self.transport.next = [self.reply(billed_usd=Decimal("0.001"))]
        self.assertEqual(self.run_one()["status"], "completed")
        self.assertEqual(self.ledger.snapshot()["charged_usd"], "0.0012")

    def test_malformed_routing_and_local_exception_cannot_authorize_model(self):
        self.router.next = [record_local_exception("fixture", "local_test", "Offline test only")]
        self.assertEqual(self.run_one()["status"], "uncertain_routing_charge")
        self.assertEqual(self.transport.calls, [])

    def test_routing_must_preserve_family_and_cannot_fall_back(self):
        self.router.receipt_overrides = {"selected_model": "anthropic/mock-opus", "selected_family": "opus"}
        result = self.run_one()
        self.assertEqual(result["status"], "routing_constraint_violation")
        self.assertEqual(result["decision"]["billed_usd"], "0.0002")
        self.assertEqual(self.catalog.calls, 0)
        self.assertEqual(self.transport.calls, [])

    def test_typed_routing_receipt_validates_exact_model_usage_and_identity(self):
        request = RoutingRequest(ALIASES["routine"], "sonnet", "synthetic", 256, ("anthropic",),
                                 required_model="anthropic/mock-sonnet-pinned")
        plan = self.router.prepare(request, self.approval)
        receipt = RoutingReceipt("fixture-decision", request.fingerprint(), "anthropic/mock-sonnet-pinned",
                                 "sonnet", Usage(10, 4), "0.0002", JEV_MODEL, "fixture")
        receipt.validate(request, plan)
        for override in ({"selected_model": "anthropic/mock-sonnet-other"},
                         {"selected_model": "openai/mock-sonnet"},
                         {"selected_model": "sk-abcdefghijk123456"},
                         {"request_sha256": "wrong"}, {"actual_router_model": "other"},
                         {"usage": Usage(9999, 4)}, {"usage": {"input_tokens": 10}},
                         {"evidence_mode": "live"}):
            with self.subTest(override=override), self.assertRaises(PilotError):
                replace(receipt, **override).validate(request, plan)

    def test_changed_approval_cannot_reuse_routing_after_generation_preflight_failed(self):
        self.catalog.overrides = {"stable": False}
        self.assertEqual(self.run_one()["status"], "invalid_catalog_or_privacy")
        original = self.approval
        for override in ({"max_output_tokens": 128}, {"allowed_providers": frozenset({"other"})},
                         {"max_attempts": 1}, {"expires_at": 2100}, {"timeout_seconds": 20}):
            with self.subTest(override=override):
                self.approval = replace(original, **override)
                self.assertEqual(self.run_one()["status"], "idempotency_conflict")
        self.assertEqual(len(self.router.calls), 1)
        self.assertEqual(self.transport.calls, [])

    def test_fixture_routing_never_authorizes_live_generation(self):
        self.transport.evidence_mode = "live"
        self.assertEqual(self.run_one()["status"], "routing_evidence_mismatch")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.ledger.snapshot()["charged_usd"], "0.0002")
        self.assertEqual(self.ledger.snapshot()["reserved_usd"], "0")

    def test_jev_control_request_is_nonrecursive(self):
        def recursive(**kwargs):
            with jev_selection_scope():
                raise AssertionError("nested selector must never execute")
        self.router.decide = recursive
        self.assertEqual(self.run_one()["status"], "recursive_routing_blocked")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.ledger.snapshot()["reserved_usd"], "0.01")

    def test_routing_exceeds_ceiling_stops_before_generation(self):
        self.router.receipt_overrides = {"billed_usd": "0.02"}
        self.assertEqual(self.run_one()["status"], "charge_exceeded_reservation")
        self.assertEqual(self.ledger.snapshot()["charged_usd"], "0.02")
        self.assertEqual(self.transport.calls, [])

    def test_routing_and_generation_share_cap_without_reserved_cost_discount(self):
        self.ledger.claim("prior", "fixture", Decimal("4.99"))
        self.ledger.finish("prior", {"status": "completed"}, billed="4.99")
        self.router.receipt_overrides = {"billed_usd": "0.01"}
        self.assertEqual(self.run_one()["status"], "budget_exhausted")
        self.assertEqual(len(self.router.calls), 1)
        self.assertEqual(self.ledger.snapshot()["charged_usd"], "5")
        self.assertEqual(self.binding.calls, 0)

    def test_proved_unsent_routing_does_not_retry_or_fall_back(self):
        self.router.next = [Failure(True, True)]
        self.assertEqual(self.run_one()["status"], "routing_not_sent")
        self.assertEqual(self.run_one()["status"], "routing_not_sent")
        self.assertEqual(len(self.router.calls), 1)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.ledger.snapshot()["charged_usd"], "0")

    def test_routing_deadline_breach_accounts_cost_and_blocks_generation(self):
        original = self.router.decide
        def slow(**kwargs):
            self.clock.now += 31
            return original(**kwargs)
        self.router.decide = slow
        self.assertEqual(self.run_one()["status"], "routing_deadline_exceeded")
        self.assertEqual(self.ledger.snapshot()["charged_usd"], "0.0002")
        self.assertEqual(self.transport.calls, [])

    def test_expired_routing_decision_cannot_authorize_generation(self):
        original = self.catalog.resolve
        def slow(*args, **kwargs):
            self.clock.now += 101
            return original(*args, **kwargs)
        self.catalog.resolve = slow
        self.assertEqual(self.run_one()["status"], "routing_decision_expired")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(len(self.router.calls), 1)

    def test_routing_failures_still_count_toward_report_cost(self):
        self.router.receipt_overrides = {"selected_family": "opus"}
        report = self.pilot.run_predefined_pairs()
        self.assertEqual(report["known_routing_subtotal_billed_usd"], "0.0002")
        self.assertEqual(report["known_api_subtotal_billed_usd"], "0.0002")
        self.assertIsNone(report["api_cost_per_accepted_fixture_task_usd"])

    def test_no_cli_key_read_or_provider_implementation(self):
        import inspect
        import live_pilot
        source = inspect.getsource(live_pilot)
        for forbidden in ("os.environ", "getenv(", "load_dotenv", "import requests", "import httpx", "urllib.request", "subprocess"):
            self.assertNotIn(forbidden, source)

    def test_non_synthetic_requests_rejected_before_binding(self):
        for args in (("codex", BRIEFS[0].id, "baseline"), (SURFACES[0], "user-brief", "baseline"),
                     (SURFACES[0], BRIEFS[0].id, "custom")):
            self.assertEqual(self.pilot.run_one(*args)["status"], "unknown_synthetic_case")
        self.assertEqual(self.binding.calls, 0)

    def test_approval_invariants(self):
        for override in ({"cap_usd": "5.01"}, {"cap_usd": "NaN"}, {"synthetic_only": False},
                         {"expires_at": 999}, {"timeout_seconds": 61}, {"max_attempts": 3},
                         {"max_output_tokens": True}, {"allowed_providers": frozenset()},
                         {"endpoint": "https://unapproved.invalid"}, {"jev_routing_approved": False}):
            with self.subTest(override=override):
                self.approval = replace(self.approval, **override)
                self.assertNotEqual(self.run_one()["status"], "completed")
                self.approval = Approval("one-total-approved-pilot", "5", frozenset({"anthropic"}), 2000,
                                         jev_routing_approved=True)
        self.assertEqual(self.binding.calls, 0)

    def test_catalog_failures_never_bind(self):
        for override in ({"provider": "other"}, {"stable": False}, {"expires_at": 999},
                         {"observed_at": 500}, {"data_collection_deny": False},
                         {"zero_data_retention": False}, {"supports_required_parameters": False},
                         {"input_price": None}, {"cache_write_price": "NaN"},
                         {"input_token_upper_bound": 0}, {"context_tokens": 10},
                         {"model": "openai/model"}, {"family": "opus"},
                         {"model": "anthropic/claude-opus-mock", "family": "sonnet"},
                         {"catalog_version": "sk-abcdefghijk123456"}):
            with self.subTest(override=override):
                self.catalog.overrides = override
                self.assertNotEqual(self.run_one()["status"], "completed")
        self.assertEqual(self.binding.calls, 0)

    def test_target_payload_and_real_receipt_fields(self):
        result = self.run_one()
        self.assertEqual(result["status"], "completed")
        sent = self.transport.calls[0]
        self.assertEqual(sent["endpoint"], ENDPOINT)
        self.assertEqual(sent["timeout_seconds"], 30)
        self.assertEqual(sent["payload"]["max_tokens"], 256)
        provider = sent["payload"]["provider"]
        self.assertEqual(provider["only"], ["anthropic"])
        self.assertFalse(provider["allow_fallbacks"])
        self.assertTrue(provider["zdr"])
        self.assertEqual(provider["data_collection"], "deny")
        self.assertEqual(result["requested_alias"], ALIASES["routine"])
        self.assertEqual(result["actual_model"], "anthropic/mock-sonnet")
        self.assertEqual(result["actual_provider"], "anthropic")
        self.assertEqual(result["provider_execution_evidence"], "fixture")
        self.assertNotIn("text", result)
        self.assertNotIn("prompt", result)

    def test_idempotent_across_restart(self):
        first = self.run_one()
        self.pilot.ledger = PilotLedger(self.path, self.approval)
        self.assertEqual(self.run_one(), first)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.binding.calls, 1)
        self.assertEqual(len(self.router.calls), 1)

    def test_idempotency_change_fails_no_spend(self):
        self.run_one()
        self.catalog.overrides = {"model": "anthropic/mock-sonnet-new"}
        self.assertEqual(self.run_one()["status"], "routing_constraint_violation")
        self.assertEqual(len(self.transport.calls), 1)

    def test_shared_five_dollar_cap_includes_all_profiles(self):
        for index, amount in enumerate(("2", "2")):
            self.assertEqual(self.ledger.claim(str(index), "same", Decimal(amount))["status"], "claimed")
            self.ledger.finish(str(index), {"status": "completed"}, billed=amount)
        self.assertEqual(self.ledger.claim("last", "same", Decimal("1"))["status"], "claimed")
        self.ledger.finish("last", {"status": "completed"}, billed="1")
        self.assertEqual(self.run_one(SURFACES[1])["status"], "budget_exhausted")
        self.assertEqual(self.ledger.snapshot()["charged_usd"], "5")
        self.assertEqual(self.binding.calls, 0)

    def test_atomic_single_flight_and_pending_no_replay(self):
        def reserve(i):
            return PilotLedger(self.path, self.approval).claim(str(i), "fingerprint", Decimal("3"))
        with ThreadPoolExecutor(max_workers=8) as pool:
            states = [r["status"] for r in pool.map(reserve, range(8))]
        self.assertEqual(states.count("claimed"), 1)
        self.assertEqual(states.count("operation_pending_no_replay"), 7)
        self.assertEqual(self.ledger.snapshot()["reserved_usd"], "3")
        self.assertEqual(self.run_one()["status"], "operation_pending_no_replay")

    def test_new_approval_cannot_reset_ledger(self):
        with self.assertRaises(PilotError):
            PilotLedger(self.path, replace(self.approval, approval_id="new-id"))
        with self.assertRaises(PilotError):
            PilotLedger(self.path, replace(self.approval, cap_usd="4"))
        with self.assertRaises(PilotError):
            PilotLedger(Path(":memory:"), self.approval)
        with self.assertRaises(PilotError):
            PilotLedger(Path(self.tmp.name) / "bad", replace(self.approval, cap_usd="6"))

    def test_unknown_error_halts_all_and_holds_reservation(self):
        self.transport.next = [RuntimeError("DO-NOT-LEAK-SECRET")]
        result = self.run_one()
        self.assertEqual(result["status"], "uncertain_charge")
        self.assertGreater(Decimal(self.ledger.snapshot()["reserved_usd"]), 0)
        self.assertEqual(self.run_one(SURFACES[1])["status"], "uncertain_charge")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertNotIn("DO-NOT-LEAK", json.dumps(self.ledger.snapshot()))

    def test_missing_billing_halts_not_assumed_zero(self):
        self.transport.next = [self.reply(billed_usd=None)]
        self.assertEqual(self.run_one()["status"], "uncertain_charge")
        self.assertEqual(self.ledger.snapshot()["halted"], "uncertain_charge")

    def test_only_proved_not_sent_gets_one_retry(self):
        self.transport.next = [Failure(True, True)]
        result = self.run_one()
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["attempts"][0]["billed_usd"], "0")
        self.assertEqual(result["attempts"][0]["input_tokens"], 0)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(self.transport.calls[0]["operation_id"], self.transport.calls[1]["operation_id"])

    def test_partial_proof_does_not_retry(self):
        self.transport.next = [Failure(True, False)]
        self.assertEqual(self.run_one()["status"], "uncertain_charge")
        self.assertEqual(len(self.transport.calls), 1)

    def test_revocation_checked_before_first_send_and_retry(self):
        calls = [self.approval, None]
        self.pilot.authorizer = lambda: calls.pop(0)
        self.assertEqual(self.run_one()["status"], "authorization_unavailable")
        self.assertEqual(len(self.transport.calls), 0)

    def test_provider_withdrawal_blocks_safe_retry(self):
        withdrawn = replace(self.approval, allowed_providers=frozenset({"other"}))
        calls = [self.approval, self.approval, self.approval, withdrawn]
        self.pilot.authorizer = lambda: calls.pop(0)
        self.transport.next = [Failure(True, True)]
        self.assertEqual(self.run_one()["status"], "authorization_changed")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.ledger.snapshot()["charged_usd"], "0.0002")

    def test_exception_code_never_leaks_arbitrary_data(self):
        def rejected():
            raise PilotError("sk-abcdefghijk123456")
        self.pilot.authorizer = rejected
        self.assertEqual(self.run_one(), {"status": "preflight_unavailable"})
        self.assertEqual(self.binding.calls, 0)

    def test_secret_shaped_generation_id_never_persisted(self):
        self.transport.next = [self.reply(generation_id="sk-abcdefghijk123456")]
        self.assertEqual(self.run_one()["status"], "actual_target_unverified")
        self.assertNotIn("sk-abcdefghijk123456", json.dumps(self.ledger.snapshot()))
        self.assertNotIn(b"sk-abcdefghijk123456", self.path.read_bytes())

    def test_changed_pair_target_suppresses_subtotal_comparison(self):
        baseline = self.run_one()
        self.catalog.overrides = {"input_price": "0.0000011"}
        optimized = self.run_one(arm="optimized")
        report = self.pilot.report([baseline, optimized])
        pair = report["pairs"][0]
        self.assertFalse(pair["api_subtotals_comparable"])
        self.assertIsNone(pair["api_subtotal_token_delta_baseline_minus_optimized"])
        self.assertIsNone(pair["api_subtotal_usd_delta_baseline_minus_optimized"])

    def test_retry_limit_is_bounded_and_replay_does_not_retry(self):
        self.transport.next = [Failure(True, True), Failure(True, True)]
        self.assertEqual(self.run_one()["status"], "not_sent_retry_limit")
        self.assertEqual(self.run_one()["status"], "not_sent_retry_limit")
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(self.ledger.snapshot()["charged_usd"], "0.0002")

    def test_overcharge_recorded_and_halts(self):
        self.transport.next = [self.reply(billed_usd="6")]
        self.assertEqual(self.run_one()["status"], "charge_exceeded_reservation")
        self.assertEqual(self.ledger.snapshot()["charged_usd"], "6.0002")
        self.assertEqual(self.run_one(SURFACES[1])["status"], "charge_exceeded_reservation")
        self.assertEqual(len(self.transport.calls), 1)

    def test_model_provider_mismatch_halts_with_charge(self):
        self.transport.next = [self.reply(actual_provider="not-approved")]
        self.assertEqual(self.run_one()["status"], "actual_target_unverified")
        self.assertEqual(self.ledger.snapshot()["charged_usd"], "0.0012")
        self.assertEqual(self.run_one(SURFACES[1])["status"], "actual_target_unverified")

    def test_bad_usage_halts_and_tracks_known_charge(self):
        self.transport.next = [self.reply(usage=Usage(20, 30, 21))]
        self.assertEqual(self.run_one()["status"], "invalid_usage")
        self.assertEqual(self.ledger.snapshot()["charged_usd"], "0.0012")

    def test_finish_length_fails_acceptance_without_extra_generation(self):
        self.transport.next = [self.reply(finish_reason="length")]
        result = self.run_one()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["accepted"])
        self.assertEqual(len(self.transport.calls), 1)

    def test_transport_deadline_breach_stops_and_accounts(self):
        original = self.transport.send
        def slow(**kwargs):
            self.clock.now += 31
            return original(**kwargs)
        self.transport.send = slow
        self.assertEqual(self.run_one()["status"], "transport_deadline_exceeded")
        self.assertEqual(self.ledger.snapshot()["charged_usd"], "0.0012")

    def test_bounded_pairs_report_exclusions_not_native_savings(self):
        report = self.pilot.run_predefined_pairs()
        self.assertEqual(len(self.transport.calls), 8)
        self.assertEqual(len(report["pairs"]), 4)
        self.assertEqual(report["actual_codex_runtime"], "unrun")
        self.assertEqual(report["actual_dot_runtime"], "unrun")
        self.assertEqual(report["actual_jev_decision_runtime"], "unrun")
        self.assertEqual(report["net_savings_claim"], "unavailable")
        for pair in report["pairs"]:
            self.assertIsNone(pair["net_workflow_token_savings"])
            self.assertGreater(pair["api_subtotal_token_delta_baseline_minus_optimized"], 0)
        for result in report["results"]:
            self.assertIsNone(result["parent_overhead"]["tokens"])
            self.assertEqual(result["decision"]["mode"], "jev_required")
            self.assertEqual(result["decision"]["evidence_mode"], "fixture")
            self.assertEqual(result["usage"]["reasoning_tokens"], 3)
        self.pilot.run_predefined_pairs()
        self.assertEqual(len(self.transport.calls), 8)
        self.assertEqual(len(self.router.calls), 8)
        self.assertEqual(Decimal(report["known_routing_subtotal_billed_usd"]), Decimal("0.0016"))
        self.assertEqual(Decimal(report["known_api_subtotal_billed_usd"]), Decimal("0.0096"))
        self.assertLessEqual(Decimal(report["budget"]["charged_usd"]), CAP_USD)


if __name__ == "__main__":
    unittest.main()
