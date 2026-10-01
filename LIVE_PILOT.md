# Bounded synthetic live-pilot preparation

Status: **code and offline fixture tests only**. No live OpenRouter request has
been run by this harness. No key was read, copied, mounted, saved, printed or
provisioned. No deployment or access grant was performed. The separately scoped
pilot spending ceiling is **USD 5 total across the entire pilot**, not per call,
profile, brief, retry, process or restart. `WritingService`, MCP defaults, existing
decision transport and production dispatch are not activated by this harness.

`live_pilot.py` is a dependency-injected controller using the shared
`routing_policy.py` recursion guard (and its privacy helper), with no HTTP client,
environment reader, credential loader, command-line enable switch, or production
adapter. Calling `LivePilot()` returns `live_adapter_unprovisioned`. The tests
provide no-network fixtures; routing receipts say `evidence_mode: fixture` and
generation receipts say `provider_execution_evidence: fixture`. A configured
generation transport still cannot run without an injected trusted JEV router;
that case returns `jev_router_unprovisioned`. Passing tests are not live-model or connected-host verification.

## Exact live activation prerequisites

A trusted operator must provide all five dependencies, outside any user-controlled
MCP request body. This repository does not provision them:

1. **Trusted authorizer** that verifies the bounded approval, the canonical pilot
   approval ID, expiration, synthetic-only content, OpenRouter destination and
   specific approved inference providers. The USD 5 ceiling by itself does not
   authorize a provider, persistent credential access or unrelated content.
   `jev_routing_approved` must explicitly authorize routing through the fixed
   OpenRouter `/api/v1/systemone` endpoint and `jev-latest` routing model as well
   as generation. Authorization is refreshed immediately before every routing
   request and generation send, including a retry;
   revocation, expiry and any scope change stop the pilot.
2. **Canonical durable SQLite ledger** shared by both profiles and all restarts.
   Reconcile any earlier actual pilot charges into the canonical budget before
   activation; this fixture-only work does not grant a fresh USD 5 allowance.
   Do not copy, erase, rotate, create a second ledger, or invent a new approval ID
   to recover budget. All processes must use the same local file. Multiple hosts
   need one approved central coordinator; copying SQLite between machines is not
   a shared cap. A ledger binds one approval and refuses cap/identity changes.
3. **Reviewed fresh catalog adapter** obtaining metadata for the exact model
   selected by JEV, constrained to the requested Sonnet/Opus family. It must not
   select an alias fallback. Metadata includes approved provider endpoints, capabilities, privacy
   metadata, full USD-per-token prices, cache prices, request fees, and a proved
   prompt-token upper bound including chat-template overhead. Missing values are
   blockers, never zero. The fixture tokenizer bound is not a production bound.
   Provider-level prices must cover the exact eligible endpoint, including any
   token-tier pricing. Observe catalog data within five minutes and its expiry.
4. **Supported preexisting secure credential binding plus reviewed transport**.
   `SecureTransportBinding.bind_preexisting(approval)` is an interface only. A
   supported binding keeps credentials opaque, binds only existing authorized
   injection, and returns an authenticated transport. No agent-side key read,
   environment discovery, secret-manager query, `.env` fallback, key file, URL
   token, new OAuth grant or persistent access expansion is implemented or
   implied. If the runtime cannot supply this binding, the live run stays blocked.
5. **Reviewed trusted JEV router** implementing the two-stage `JevRouter` protocol.
   `prepare(request, approval)` is strictly local and non-billable: no network,
   credential read or binding, routing SDK invocation, or provider lookup. It
   returns a fresh typed `RoutingPlan` with a proved inclusive maximum USD charge,
   token bounds, expiry, fixed endpoint/model and evidence mode. After atomic
   reservation, `decide(...)` may make exactly one bounded JEV control request
   using supported preexisting secure injection. It must disable SDK auto-retries,
   recursive routing, redirects, fallback hosts/models and unmetered calls. It
   returns a typed `RoutingReceipt` with a request-bound decision ID, selected
   concrete model/family, native routing usage and definitive billing. No default
   router, credential discovery, raw SDK adapter or live implementation is supplied.

JEV selection is required for every new pilot model execution. The immutable
routing request contains the synthetic task, requested alias/family, generation
output limit and allowed providers. Exact-model constraints, when supplied by a
trusted caller, must also match the decision; they cannot be relaxed by JEV. The
routing operation fingerprint additionally binds the entire approval, including
identity, cap, expiry, provider set, output/retry limits and timeout. Changing
these after a route-only success causes an idempotency conflict, not reuse under
changed authority. Routing expiry is rechecked before generation and safe retry.
A proved-unsent generation retry reuses its still-valid decision for the same
operation, never creates a fresh unreserved routing call.

The internal JEV control request is the routing primitive, so it is not routed
through JEV again. The shared selection guard blocks selector reentry. There is
no implicit local-model or deterministic-selection exception. A local-operation
receipt cannot authorize model execution. Missing routers, malformed receipts,
constraint violations and outages block generation without an alias fallback.
Fixture routing evidence cannot authorize a live generation transport, and mixed
routing/generation evidence modes are refused before generation is sent.

The future transport must enforce a real total request deadline (30 seconds by
default, at most 60), a bounded response body (at most 65,536 text bytes), and the
specified maximum output (256 by default, at most 512 tokens). It must disable
SDK retries, redirects, fallback models/providers, extra network calls, tools,
search/plugins, and unbounded receipt polling. Transport implementations must
receive security review and mocked contract tests before any live run. This
controller detects a returned deadline breach but cannot interrupt a misbehaving
injected transport; deadline enforcement is part of the trusted transport gate.

Requests pin the exact resolved model and provider, with `only`, `order`,
`allow_fallbacks: false`, `require_parameters: true`, `data_collection: deny`,
`zdr: true`, and price ceilings. These controls restrict routing, but do not
replace consent or prove a provider complied with its policy. No real personal,
private, repository, customer, account or credential text is accepted: callers
can select only the checked-in synthetic brief ID, arm and profile.

The transport must return a verified generation ID, actual model and provider,
native input/output usage, cache read/write and reasoning details, finish reason,
and a definitive billed charge. Missing/estimated/ambiguous billing stops the
entire run and retains its reservation. If the ordinary completion does not
provide the requisite receipt, stop for read-only reconciliation; never regenerate
the prompt to obtain a receipt. Normalization must preserve the provider's token
semantics: cached input is a subset of total input, reasoning is a subset of total
output, and detail fields must not be counted twice. Transport evidence explicitly
distinguishes `fixture` from `live`.

## Budget, concurrency and failure behavior

- Reserve routing first, before its possibly billable call or credential binding,
  using the inclusive routing plan ceiling. Settle its definitive charge, then
  reserve generation in the same ledger: input bound times input plus cache
  read/write prices, maximum output times output price, plus request fee. No
  expected caching discount is used. An unknown routing charge holds its full
  reservation and stops the entire pilot; it is never assumed to be free
- Persist reservations and settled charges as rounded-up nanodollars; total
  commitment cannot intentionally exceed the smaller authorized cap, at most USD5
- Permit one outstanding operation per ledger across processes. A pending
  operation after a crash remains blocked without replay; time passing never
  releases a reservation
- Routing and generation have separate durable operation records in the same
  ledger. Idempotency binds fixture version/profile/brief/arm, routing request and
  plan, full approval, decision ID, exact generation payload, target, catalog,
  pricing, bounds and retry settings. A completed repeat reuses its stored
  decision and generation receipt without another billable call; changed
  parameters conflict rather than regenerate
- At most one routing send and two generation send attempts per case. Retry only when the transport proves both that
  nothing was sent and that a charge was impossible. HTTP status, timeout,
  cancellation, disconnect or generic exception is not proof. There are no
  automatic quality retries or provider/model fallback
- A possible charge, malformed receipt, unexpected model/provider, privacy or
  authorization change, invalid usage, timeout breach or missing billing halts
  the run. Unknown charge retains the entire reservation
- A provider charge above the reserved bound is recorded accurately and halts all
  further work. The local ledger cannot undo a provider billing violation or
  prevent out-of-band account spending. Before claiming a guaranteed monetary
  ceiling, the secure operator must verify the provider honors these bounds or
  use a separately authorized existing provider-side spend control. Do not create
  new credentials or alter limits/security as an implicit implementation step
- No automatic reset/reconciliation mutator is provided. An authorized operator
  must inspect provider evidence before resolving unknown/pending state

The ledger stores sanitized IDs, usage, prices, acceptance flags, output hashes,
and cost/latency data. It does not store request bodies, drafts, keys or provider
exceptions. Credential-shaped metadata is refused. This screening is conservative
defense in depth, not a claim that every possible secret can be recognized.

## What the pilot can and cannot measure

The predefined design runs two artificial briefs, baseline and reduced-context
arms, repeated under `codex-profile-surrogate` and `dot-profile-surrogate` labels:
eight generations and eight routing decisions maximum, with counterbalanced arm
order across briefs. Proved-unsent generation retries remain bounded as above. Profile
names do not imply execution inside either actual host. Both arms receive the
same task and relevant facts; the baseline additionally contains predefined
irrelevant fictional facts. They use the same intended model family, output cap
and acceptance requirements. The result is a small artificial context-reduction
experiment, not a realistic sample of a user's workload.

Receipts preserve each routing decision, its request binding, selected model and
family, evidence mode, usage and billed cost; safe rejected decision metadata is
retained as well. They also preserve every generation send attempt, including
proved-zero-charge retries, all known usage/charges, cache/reasoning details, latency, finish reason and
deterministic fixture acceptance. Failed tasks still count toward the cost
denominator; known API cost per accepted fixture task is separately reported.
Read the full attempt/results list rather than only successful pairs.

API subtotal token/cost deltas are reported only when both arms have matching
actual model, provider, family, catalog version, pricing and execution evidence,
plus routing model, plan and evidence. Subtotals include routing and generation
usage/cost; the report also separates their known billed costs.
Mismatches are **incomparable**, and their deltas are null. Both-arm fixture
acceptance is reported separately; a cheaper failed answer is not established
savings at equivalent quality. The simple fact/word checks are not blinded human
quality assessment. Output text is not rewritten by an OpenAI host.

Selection is mandatory injected JEV routing, independently reserved and metered
inside the shared cap. Offline tests use synthetic JEV receipts and prove the
control/accounting contract only. They make no real call to
`/api/v1/systemone`/`jev-latest`; they cannot validate an actual router or its
model quality, host compatibility, pricing or billing. Separately obtained live
JEV evidence must remain separately attributed; this harness did not obtain it.

Parent coordination, re-reading, routing, tool and failed-work accounting must be
obtained from actual host telemetry before any end-to-end net claim. This harness
marks parent overhead unobserved and actual Codex/dot runtime measurements
**unrun**. Offline routing receipts leave actual JEV runtime **unrun** as well. A
future reviewed live router can report `live_api_receipts_only`; that label still
does not establish execution inside either host. It always reports net workflow token/cost savings as null and
`net_savings_claim: unavailable`. OpenRouter API costs are not subscription plan
usage. A live API-only result does not establish Codex/dot compatibility,
end-to-end JEV savings, acceptance quality or universal savings.

To answer the actual user goal, the remaining measurement gate is to discover
tools and telemetry on each actual supported host, obtain comparable baseline and
JEV runs on matched tasks, account for every decision/generation/retry/cache/tool
and parent token/charge, verify equivalent accepted quality, and publish the
per-surface result with exclusions. Unknown host tokens or incompatible billing
units remain a blocker to a numeric net-savings claim.

## Offline verification

Run with the real project environment through the credential-free, socket-blocked
runner (no key or provider connection required):

    python scripts/offline_tests.py -k live_pilot

`tests/test_live_pilot.py` covers unprovisioned defaults, synthetic allowlisting,
authorizer refresh/revocation, alias/family/namespace boundaries, catalog privacy,
safe metadata/errors, persistent idempotency, concurrent atomic reservations,
shared routing/generation cap, routing-before-send reservations, missing/malformed
routing bounds and receipts, exact model/family constraints, full approval
idempotency, recursion rejection, no alias/local fallback, fixture/live evidence
mismatch, routing expiry, crash-pending state, uncertain charge, bounded safe
retries, overcharge detection, actual provider/model receipts, timeout breaches,
incomparable pairs and absence of native/net-savings claims.

## Official implementation references

Rechecked read-only on 2026-09-30; live adapters must recheck before use:

- [Provider routing and privacy filters](https://openrouter.ai/docs/guides/routing/provider-selection)
- [Model catalog and pricing fields](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties)
- [Usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)
- [Sonnet latest alias](https://openrouter.ai/~anthropic/claude-sonnet-latest)

No specific current model version, current price, working live identity, privacy
availability or billable result is asserted by the fixture data.
