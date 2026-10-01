# Explicit cloud routing runner

This local operator CLI returns a validated recommendation; the dot parent still
performs any separately authorized native dispatch. It is not a new dot tool,
MCP connection, hook, OAuth service, or automatic routing installation.

From the saved environment:

```bash
cd /workspace/jev-agent
/workspace/jev-venv/bin/python scripts/route_request.py --fingerprint < /workspace/scratch/request.json
/workspace/jev-venv/bin/python scripts/route_request.py --authorization /workspace/scratch/route-authorization.json < /workspace/scratch/request.json
```

`--fingerprint` validates and normalizes the TaskEnvelope and outputs
`{"task_sha256":"..."}` without credentials or network. The routing invocation
prints one RouteRecommendation JSON object to stdout. Inspect `status`; only
`recommended` with live billed JEV evidence is suitable for parent validation.
A rejected recommendation can return exit 0 with a non-recommended status;
parse/authorization/transport setup exceptions return exit 1 and sanitized
`{"error":"live_routing_unavailable"}`. No request content or credential is
included in error output. Standard argparse help/usage remains on its usual
streams.

## Request and operator contract

The request is the existing strict `host_contracts.TaskEnvelope` JSON schema.
For example (replace ID, text, budget, and deadline for the authorized task):

```json
{
  "request_id": "unique-request-id",
  "purpose": "Summarize the supplied public facts",
  "acceptance_criteria": ["Return three factual bullets"],
  "context_references": [],
  "data_class": "public",
  "authorized_destinations": ["openrouter.ai"],
  "budget_usd": 0.002,
  "deadline": 0.0,
  "task_kind": "narrow",
  "required_tools": [],
  "parent_has_context": false
}
```

The zero timestamp is deliberately invalid. Use an actual approved future Unix
seconds timestamp. Optional explicit model/namespace/effort constraints use the
same TaskEnvelope fields and are preserved by the routing policy. This runner
accepts public content only and refuses context references. Authorization does
not exempt content from the existing privacy screening.

The operator creates `/workspace/scratch/route-authorization.json` with mode
0600, owned by the process user and not a symlink. Its exact fields are:

```json
{
  "approved_task_sha256": "<hash from --fingerprint>",
  "snapshot": {
    "surface": "dot_parent",
    "runtime_id": "<observed-runtime-id>",
    "session_id": "<observed-session-id>",
    "observed_at": 0.0,
    "expires_at": 0.0,
    "available": true,
    "observed_tools": [],
    "dispatch_controls": ["delegate", "model_override", "effort_override"],
    "models": [],
    "evidence_status": "runtime_observed"
  },
  "expires_at": 0.0,
  "routing_ceiling_usd": "<verified complete per-call upper bound>",
  "actual_router_models": ["<verified resolved typesafe/jev-version>"],
  "pricing_evidence": "<operator-evidence-id>"
}
```

This is a field guide, **not runnable approval or current model evidence**. Use
the actual allowed surface from CapabilitySnapshot and populate model entries
with exact observed `model_id`, `namespace`, `family`, `efforts`,
`supported_tools`, and `available`. Only attest controls the parent actually
exposes. Snapshot and task deadlines must fall within the authorization expiry,
which must be within five minutes of invocation. A changed task needs a new
normalized hash and explicit operator review.

The authorization file is trusted operator input, not cryptographic proof. The
parent/operator must independently establish task consent, current catalog and
controls, resolved router identity, and a complete routing price ceiling
(including request/cache/other fees and context/output bounds). A guessed budget
is not a price quote. Missing fee metadata is not proof of zero. Do not let an
untrusted task author write this file or expose its path through a public MCP
argument. Ordinary same-user filesystem permissions cannot distinguish a
trusted operator from malicious code already running as that user.

## Existing binding and budget

The runner uses the verified issued `OPENROUTER_API` placeholder through
`HTTPS_PROXY`, fixed `https://openrouter.ai/api/v1/systemone`, and `jev-latest`.
It never loads `.env` or the legacy raw-key environment variable. These existing
protected environment bindings must already be present; do not paste keys into
JSON or shell commands.

It requires `/workspace/scratch/jev-canonical-budget.sqlite` and the existing
approval ID, with at least $0.046548454 already charged. It never initializes a
missing ledger or resets the $5 aggregate cap. It reserves the verified routing
ceiling before a call and retains the existing halt/idempotency behavior.
Identical unexpired task/catalog/identity replay does not make a second paid
call. Do not change session/request IDs to retry uncertain charges. The runner
does not broaden the existing synthetic spending authorization: additional task
content and spending must be authorized before issuing its operator file.
Native worker billing remains separate and unknown here.

The parent validates the returned decision against its still-current host state
and execution permission before explicitly calling its own dispatcher. This CLI
never starts a worker or changes parent models. There were no paid calls in its
verification; offline tests replace only network I/O and use temporary ledgers.
