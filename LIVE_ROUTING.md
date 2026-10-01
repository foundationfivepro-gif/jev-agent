# Explicit live host routing

`live_routing.py` implements the fixed OpenRouter `/api/v1/systemone` transport
with model `jev-latest`, a guarded JEV selector, durable routing reservations,
server-owned host discovery, validated recommendation receipts, and an injected
host dispatch path. This is executable live code; offline tests substitute only
its network/host boundaries. One live synthetic JEV routing request has now been verified through the
protected proxy; native worker execution is pending parent dispatch. Writing tools remain disabled/mock-only.

No changes activate the existing Claude MCP configuration, remote OAuth, current
chat model, hidden calls, or Mac. A repository push can trigger the existing
Vercel preview; it does not activate this separate composition root.

## Install into an authorized host

Use Python 3.12 and the checked-in requirements. Start a separate host-owned
Python launcher with this repository on `PYTHONPATH`. The launcher imports only:

```python
from live_routing import (
    BoundHostRouting, OpenRouterSelector, ProtectedOpenRouterTransport,
    RoutingAuthorization, create_routing_server, payload_fingerprint,
)
from live_pilot import PilotLedger

transport = ProtectedOpenRouterTransport(
    issued_placeholder=host_issued_placeholder,
    https_proxy=host_https_proxy,
)
selector = OpenRouterSelector(
    transport=transport,
    ledger=canonical_ledger,
    authorize=authorize_and_quote_routing,
)
host = BoundHostRouting(
    runtime_id=authenticated_runtime_id,
    session_id=authenticated_session_id,
    discover=host_capability_snapshot,
    selector=selector.binding(),
    authorize=authorize_public_task,
    dispatch=host_dispatch_with_budget_and_durable_idempotency,
)
create_routing_server(host).run()  # stdio; recommendation tool
# Host-owned worker request entry point, outside MCP:
# result = host.run(task_envelope)
```

The named host callbacks above are required integrations, not built-in functions
or OAuth placeholders to send as tool arguments. Configure the host's supported
stdio MCP registration to launch this file with an absolute Python path. Do not
replace `.mcp.json` or register a remote endpoint without its separate grant.

Required binding fields:

- Issued protected-network placeholder source for `OPENROUTER_API`, and approved
  HTTPS CONNECT proxy endpoint/trust configuration. Never read the legacy raw
  `OPENROUTER_API_KEY`, log the placeholder, or place either in prompts/config.
  The transport has no environment reader, direct-network fallback, redirect or
  retry. Host setup attests that the callback supplies an issued placeholder.
- Authenticated runtime/session identity and exact available native model IDs,
  efforts, tools, controls, current model, and observation expiry. Discovery must
  return the same cached snapshot until it expires or the catalog changes. A
  changed timestamp/catalog requires a new request and JEV receipt.
- `authorize_public_task(task) -> bool`, backed by the actual host grant. Live
  selection additionally requires `data_class="public"` and destination
  `openrouter.ai`; tool input alone does not create consent.
- `authorize_and_quote_routing(task, payload) -> RoutingAuthorization` with the
  exact `payload_fingerprint(payload)`, positive USD ceiling, expiry, verified
  exact resolved JEV model IDs and pricing-evidence identifier. Verify the
  ceiling against current System One input/output limits and pricing. A guessed
  allowance is insufficient: reservations cannot prevent an upstream charge
  exceeding a false quote. Missing bounds block live verification.
- One durable `PilotLedger` under the existing $5 approval, shared by ALL paid
  routing and generation workers. Never use a fresh ledger to reset allowance.
  This environment's canonical path is
  `/workspace/scratch/jev-canonical-budget.sqlite`. It imports the user-verified
  previous charge **$0.046511158**, plus the verified routing call **$0.000037296**.
  Aggregate spend is **$0.046548454**; available allowance is **$4.953451546**. Do not run concurrent paid tasks elsewhere with
  another copy. Persist/reconcile this file before moving environments.
- A host dispatcher that independently enforces permissions, generation budget
  reservations/settlement in that same ledger, and durable request idempotency.
  It receives `task` and the validated `decision`; only execute the exact native
  model, effort and mode. Unknown dispatch outcomes must never be blindly
  replayed. The local in-memory guard adds protection but cannot replace the
  host's durable guard after process restart.

MCP request `runtime_id` and `session_id` must match the binding. Its supplied
`snapshot` is ignored when bound: only discovery supplies runtime evidence.
Recommendations never enable dispatch or grant permission. `host.run` requires
live billed JEV evidence, fresh unchanged discovery, and a second permission
check. It cannot switch a platform-fixed parent or intercept hidden model calls.

## Evidence and remaining activation gates

Run `/workspace/jev-venv/bin/python scripts/offline_tests.py` in this workspace.
Tests cover real SDK registration, identity spoofing, unknown billing, budget
exhaustion/overruns, receipt validation, synthetic-provenance rejection, privacy,
non-recursion, replay prevention, and catalog changes before dispatch.

The live synthetic routing smoke passed using the verified `OPENROUTER_API`
network-secret placeholder via `HTTPS_PROXY`; the raw credential never entered
this process. The legacy raw key was not accessed. Real native worker
verification still requires the parent dispatcher.
Remote clients need their separate approved OAuth issuer/audience/scopes,
principal-to-host binding and registered URL; none are provisioned here.
Latest Sonnet/Opus writing transport/catalog installation is a separate unfinished
integration; `DEFAULT_SERVICE` intentionally remains unavailable without its
independent auth/catalog/budget dependencies.

Verification on 2026-10-01: **697 passed, 7 skipped, 82 subtests passed** using the
locked dependencies and credential-free/network-blocked test runner. Independent
review identified synthetic dispatch acceptance, broad model identity, missing
quote evidence, and `NO_PROXY` bypass. These were corrected and regression-tested.
Live calls: **1**, billed **$0.000037296**. See the [complete validated
receipt](evidence/live-native-route-2026-10-01.json). JEV selected `gpt-6-luna`,
`low` effort, `delegate`; actual router `typesafe/jev-1.13-20260917`, 888 input and
80 output tokens. The imported prior pilot charge remains separately identified.

Fresh endpoint metadata reports $0.042/million input tokens, zero output cost,
and a 32,000-token context. Reserving the entire input context established a
conservative **$0.001344** request bound, released on settlement. Only one
synthetic request was sent, with no retry. Decision ID:
`gen-dec-1790821519-uh7HduqHau0t8AP85YJq`. Native task: sort `[7,2,9,2]` and sum;
expected output `{"sorted":[2,2,7,9],"sum":20}`, no tools or side effects.

Vercel preview for code commit `f8c1c188026bae22bde37c71654efb95c11b18fc`
reports `BLOCKED`, GitHub status "Deployment was blocked", and links to Vercel
team-configuration troubleshooting. GitHub resolves the commit author/committer
to `adamsavoy`. The connected Vercel team is `foundationfivepro-7852s-projects`.
The tools expose no more specific membership/plan error, and the build-log tool
is unavailable. No team membership, account links, grants or settings changed.
