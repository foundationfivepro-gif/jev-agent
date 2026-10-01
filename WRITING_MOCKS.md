# Writing service: offline preparation only

`writing_service.py` implements the bounded plan/generate contracts for synthetic
testing. `writing_auth.py` implements an independent mock client-to-service OAuth
boundary. Neither module supplies an HTTP transport, reads an upstream credential,
discovers a live issuer/provider, creates a real grant, or changes existing Claude
integration. `DEFAULT_SERVICE` is disabled. Enabling a service instance accepts
only the concrete offline mock catalog, auth, ledger, JEV selector and transport
fixtures. This implements the mandatory-JEV policy in an isolated mock writing
boundary; it does not change live routing or authorize a live call.

This is **not deployable production authentication or billing infrastructure**.
Tests demonstrate local Python behavior, not tool discovery, host OAuth support,
real provider privacy, actual pricing, output quality, or savings on any surface.

## Shared integration boundary

Both MCP servers wrap these same entry points:

```python
plan_writing(request: PlanRequest | dict, *, access_token: str | None = None) -> dict
generate_writing(request: GenerationRequest | dict, *, access_token: str | None = None) -> dict
```

The access token comes only from trusted host request context, never a tool
argument. Default unconfigured calls return `unavailable`. Strict Pydantic
`PlanRequest`, `GenerationRequest`, `WritingPlan`, `WritingDecisionRequest`,
`WritingDecisionReceipt`, and `GenerationReceipt` schemas
forbid extra fields and include explicit versions. Costs use exact Decimal
arithmetic and JSON decimal strings in USD.

A plan request contains a request ID, bounded brief, routine/complex/ambiguous
classification, optional explicit Sonnet/Opus family, data class, destination
allowlist, budget, output-token limit, and absolute UTC deadline. It does not
contain credentials or a caller-asserted grant. Generation accepts only plan ID
and request ID. Server-side principal grants bind each destination and content
class; tool arguments can narrow those grants but cannot create them.

## Model, consent and data controls

- Every new writing-generation intent requires one injected mock JEV decision,
  including clear/routine/complex briefs and explicit Sonnet/Opus choices. The
  selector receives bounded routing metadata and a request fingerprint, never the
  raw brief. Complexity is context, not a deterministic local selection rule
- JEV's synthetic outcome selects `~anthropic/claude-sonnet-latest` or
  `~anthropic/claude-opus-latest`. An explicit family is a hard constraint supplied
  to the selector: disagreement returns `decision_conflict` and no generation.
  Missing JEV returns `decision_unavailable`; malformed, unrelated or incorrectly
  versioned receipts return `invalid_decision`. No case silently falls back
- The internal JEV selector call uses the shared non-recursive selection guard.
  Re-entering selection returns `recursive_routing_blocked`. Direct local
  execution is reserved for non-model operations such as local file I/O, builds
  and tests; even a local model call must use JEV, and this service accepts no
  model-generation exception or caller-supplied decision receipt
- The official-family alias and stable identity come from supplied metadata, not
  display names, version sorting or a hard-coded production model. These test
  fixtures are intentionally fake and must never be treated as live metadata
- Generation refreshes metadata once, revalidates capabilities, pricing, consent,
  deadline and provider privacy, and blocks an alias target change. A price change
  within the approved cap is recorded; missing prices remain unknown and block
- OpenRouter and the specific selected provider must both be approved for the
  declared content category. Requests pin the provider and require
  `data_collection=deny`, `zdr=true`, and `allow_fallbacks=false`. A missing/stale
  privacy declaration blocks; filters themselves do not confer consent
- The repository secret classifier, with inline allowlisting disabled, screens
  raw briefs and raw drafts. Additional checks reject short/quoted credential
  assignments. Authorized ordinary personal writing is supported; a heuristic
  detector cannot establish that arbitrary prose is safe or correctly classified
- The exact safe mocked draft is returned, including whitespace and Unicode.
  There is no rewrite call or safety-refusal fallback

## Accounting, retries and retention

The in-process ledger atomically reserves before generation and isolates tenant
budgets. Generation IDs are idempotent, conflicting replays fail, and each plan
can authorize only one generation ID. A replay returns sanitized receipt/status,
not a cached private draft. Every new plan accounts for its modeled JEV selection
charge, even if catalog/consent checks later prevent generation. A decision is
bound to tenant, subject, request ID, and request fingerprint. Plan replay reuses
that decision; changing the intent under the same ID fails. Selection failures
are also retained to prevent rerouting or double charging. There is a
256-plan/decision tenant quota and four-inflight limit.

One transport retry is allowed only when the mock explicitly proves both that the
request was not sent and that a charge is impossible. Retry delay is bounded to
five seconds and the task deadline; auth revocation, catalog, price, consent and
privacy are rechecked. Retries reuse the identical routed generation payload and
decision ID without calling JEV again. There is no retry for an acknowledged timeout, partial
stream, malformed receipt or otherwise unknown charge. Such outcomes retain the
reservation. No mock status-reconciliation endpoint is provided; production
reconciliation is an unimplemented rollout prerequisite. Billing over the reserved
amount is recorded as uncertain, retains the charge, and freezes the tenant.

Receipts distinguish billed, estimated and uncertain cost; account for generation,
decision, tool and cache usage; and include actual returned model, provider,
versions, retry count, finish reason, latency and acceptance status. Every planned
generation includes its validated JEV decision ID and sanitized decision receipt
in both plan and generation receipt; transport metadata carries the same ID and
routing-policy version. Unknown transport exceptions retain that provenance and
the budget reservation. `not_reviewed`
means no human quality/acceptance decision has happened. Captured mock requests
omit messages, raw briefs and draft content. Submitted briefs live in transient
memory while a plan is pending and are discarded on terminal/uncertain completion.
Private response caching, raw-content traces and durable storage are absent.

## Mock OAuth boundary and production gates

The mock advertises protected-resource and authorization metadata, header-only
bearer transport, authorization-code flow, and S256 PKCE. Synthetic codes bind
registered client, exact HTTPS redirect URI, requested resource, challenge and
expiry; they are single-use. Tokens bind issuer, audience, tenant, subject, scopes
and finite expiry. Scope checks and revocation apply before planning, generation
and receipt replay. TTL is finite, positive and capped; credential-bearing URL
configurations and query-token workarounds are rejected. The upstream OpenRouter
key is never accepted as a client token.

Before any live activation, separately approve and implement at least:

1. Hosting, issuer and key-verification ownership; actual host discovery and OAuth
   conformance, including state/redirect handling and client registration policy
2. Durable tenant/subject grants, revocation, rate limits, per-user quotas and
   distributed atomic idempotency/reservations across workers and restarts
3. Exact OpenRouter/provider/content consent, fresh metadata and verified privacy
   behavior, secure credential provisioning, and capped pilot spending
4. Retention/deletion rules, pending-plan expiry cleanup, incident ownership,
   charge reconciliation and circuit breakers
5. Live transport implementation and independent review, live discovery and
   bounded generation on each intended surface, deployment and rollback approval

Nothing in this mock implementation or a successful unit test authorizes those
actions or establishes live support.

## Reproduce the offline tests

Install the repository dependencies in an isolated environment. Run with no
provider credentials:

```sh
python -m unittest discover -s tests -p test_writing_service.py -v
# or
python -m pytest tests/test_writing_service.py -q
```

The suite blocks sockets and uses only synthetic tokens, briefs, catalogs, JEV
decisions and provider responses. `scripts/portability_evidence.py` asserts one
mock JEV decision for each of its 24 synthetic cases and matching decision IDs
across plans, generation receipts and captured transports. Coverage maps to
U08–U12, S06–S11, I03–I04, and F02–F08 of the
approved plan, with negative schema, Unicode-size, NaN/Infinity, concurrency,
revocation, short-secret, mandatory-selection, explicit-constraint conflict,
recursion, receipt-binding, retry-reuse and unknown-charge cases. These are mock test claims;
real grants, live transport, actual billing and per-host support remain unverified.
