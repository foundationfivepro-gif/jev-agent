# Concrete OpenRouter local operator

`local_operator.build_operator(config_path)` implements all operator callbacks.
No custom Python binding is required. The unified server provides only routing
recommendations, command gating and context selection; native dispatch stays in
the caller. This release has not been installed or activated on the Mac.

## Minimal launch flow

1. Use the reviewed release snapshot and a Python environment with its existing
   requirements installed. Keep the clean Mac project checkout untouched.
2. Write a private, process-owner-owned mode-0600 JSON configuration matching
   `config/local-operator.schema.json`. This file contains approvals, observations
   and prices, **never credentials**. The sample `config/local-operator.example.json`
   is deliberately expired and has no task permissions. It is not authorization.
3. Fill in current observed host identities/catalog/controls, explicit authorized
   task/tool hashes, verified complete prices, and the already-authorized ledger
   path. Do not copy, initialize or reset the cloud-owned ledger. The pending
   Mac smoke instead requires a distinct explicit approval ID and cap <= $0.10.
   Until that approval is received, do not initialize or use a live Mac ledger.
4. Run the non-spending config check:

```text
/absolute/venv/bin/python /absolute/release/scripts/unified_mcp_server.py --config /absolute/private/operator.json --check-config
```

5. After the credential and access prerequisites are approved, the host launches
   this same command **without** `--check-config` under its secure injector.
   For the verified Mac interface, `/opt/homebrew/bin/op run` supplies
   `JEV_OPENROUTER_TOKEN` to the separate STDIO child. Keep `op run` output masking
   enabled; do not put a key in command arguments or JSON. The host owns the
   exact approved secret reference and authentication. The server never invokes
   `op`, searches vaults, calls `launchctl`, reads `.env`, or looks for credentials
   in another variable/provider.

The existing Mac `op` binary supports transient reference injection, but its
account authentication has **not** been verified (`op whoami` failed). That is
an access prerequisite, not a reason to switch to Gateway. The separate Gateway
`jev.mjs` launcher is not used.

No persistent registration is performed by these commands. Proposed install
location is `~/.codex/integrations/jev-openrouter`; applying that installation and
an MCP registration remains a separate approved action.

## Separate smoke-ledger setup (approval still pending)

Only after the separate Mac synthetic smoke is approved, set its actual distinct
approval ID/evidence, cap <= `.10`, `budget_scope="local_synthetic_smoke"`,
`minimum_charged_usd="0"`, and `ledger_creation_authorized=true`. Then run:

```text
/absolute/venv/bin/python /absolute/release/scripts/unified_mcp_server.py --config /absolute/private/operator.json --initialize-ledger
```

This setup command needs no credential and makes no model calls. It creates only
a new mode-0600 ledger at the exact approved path and refuses any existing file.
The action-time approval must bind this exact ledger path and configuration hash.
Never reuse one approval ID to create a second ledger elsewhere; no distributed
approval registry is implemented. Set `ledger_creation_authorized=false` afterward. It never imports spending from,
transfers, copies, resets or modifies the canonical cloud ledger. The cloud
approval cannot authorize this new smoke budget. This release does not execute
that setup on either host; its tests create temporary fixture ledgers only.

## Configuration contract

Print the exact schema without secrets or ledger access:

```text
/absolute/venv/bin/python /absolute/release/scripts/unified_mcp_server.py --schema
```

Top-level fields:

| Field | Meaning |
|---|---|
| `schema_version` | `1.0` |
| `transport` | `direct_env` for the Mac host-injected OpenRouter token; `protected_proxy` only for an existing verified proxy-placeholder environment |
| `https_proxy` | Omit/null in direct mode; explicitly approved proxy URL in proxy mode |
| `ledger_path` | Absolute path to the existing canonical SQLite ledger |
| `budget_scope` | `canonical_cloud` or separately approved `local_synthetic_smoke` |
| `approval_evidence` | Reference to the actual operator approval; a pending request is not approval |
| `ledger_approval_id`, `ledger_cap_usd` | Cloud: existing ID and `5`; Mac smoke: distinct approval ID and cap <= `.10` |
| `minimum_charged_usd` | Cloud: at least `0.046548454`; new separately approved smoke: `0` |
| `ledger_creation_authorized` | Defaults false; true only after explicit permission for a new local smoke ledger |
| `approval_expires_at` | Unix seconds, current and at most five minutes away |
| `workspace_roots` | Exact absolute roots; initial Mac scope identified by parent is `/Users/administrator/Documents/Codex/2026-10-01/task` |
| `snapshot` | Actual current CapabilitySnapshot, including supported model IDs/efforts/tools/controls and session/runtime identities |
| `catalog_evidence` | Identifier linking the operator's actual host observation |
| `prices` | Full fee schedule and resolved router identities described below |
| `tasks` | Exact normalized routing-task hashes, expiry, and explicit synthetic-content attestation |
| `operations` | Exact gate/context argument hashes, stable operation IDs, total-operation ceilings, expiry, and synthetic/public-metadata attestations |

Both catalog and prices must have been observed within five minutes and remain
unexpired within the enclosing approval. The private config is reread before
requests/subcalls so revocations take effect. Runtime/session, transport, roots
and ledger identity cannot be changed in place; restart with newly approved
configuration. A changed grant during batching halts the operation before
another model request. Removing grants denies requests. No broad wildcard grants.

Compute consent hashes with the matching tool name and stdin JSON:

```text
/absolute/venv/bin/python /absolute/release/scripts/unified_mcp_server.py --fingerprint jev_recommend_host_route < task.json
/absolute/venv/bin/python /absolute/release/scripts/unified_mcp_server.py --fingerprint jev_gate_command < gate-arguments.json
/absolute/venv/bin/python /absolute/release/scripts/unified_mcp_server.py --fingerprint jev_select_context < context-arguments.json
```

The result is `{"sha256":"..."}`. Hash generation is validation, not approval.
The operator adds that hash only after reviewing the exact request. Routing uses
TaskEnvelope; gate uses `command` and default `cwd="."`; context uses `goal`,
`root`, default `globs=null` and `budget_tokens=40000`. Normalized defaults are
included in the hash. Authorization files are operator authority, not signatures
or independent proof of consent. Untrusted task code must not control them.

The existing generic `prices` profile requires `observed_at`, `expires_at`, `evidence`, `actual_models`,
`context_tokens`, `prompt_per_token`, `completion_per_token`,
`cache_read_per_token`, `cache_write_per_token`, `request_fee`, and
`other_fees_upper_usd`. Every rate/fee is an explicit nonnegative decimal string;
missing fields are rejected. The operator must verify actual System One pricing,
resolved `typesafe/jev-*` identities, context/output bounds, and all applicable
auxiliary charges. Missing metadata does not mean zero. The adapter calculates a
conservative full-context bound by summing all token rates, multiplying by the
verified context bound (at most 32000), and adding request/other fee bounds.
This is validation of operator-supplied evidence, not independent price discovery.

### Mac migration: documented Jev input-only tariff

Keep the generic profile unchanged when a complete fee schedule is available.
For the documented Jev 1.13 System One input-only contract, replace the entire
`prices` object with this shape, substituting freshly observed timestamps and an
actual evidence reference (the times below are deliberately expired):

```json
{
  "profile": "jev_systemone_input_only",
  "observed_at": 1.0,
  "expires_at": 2.0,
  "evidence": "REPLACE_WITH_FRESH_PUBLIC_METADATA_EVIDENCE",
  "alias": "~typesafe/jev-latest",
  "alias_target": "typesafe/jev-1.13",
  "canonical_model": "typesafe/jev-1.13-20260917",
  "endpoint_model_id": "typesafe/jev-1.13",
  "provider_name": "TypeSafe",
  "supports_implicit_caching": false,
  "context_tokens": 32000,
  "actual_models": ["typesafe/jev-1.13-20260917"],
  "alias_pricing": {"prompt": "0.000000042", "completion": "0"},
  "model_pricing": {"prompt": "0.000000042", "completion": "0"},
  "endpoint_pricing": {"prompt": "0.000000042", "completion": "0", "discount": 0}
}
```

Use these unauthenticated metadata sources together:

- [Alias](https://openrouter.ai/api/v1/model/~typesafe/jev-latest): `alias_target.slug`, context and pricing.
- [Model](https://openrouter.ai/api/v1/model/typesafe/jev-1.13): canonical slug, context and pricing.
- [Endpoint](https://openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints): model ID, provider, context, implicit caching and pricing.

The operator must verify all three observations, with `observed_at` set to the
oldest observation and expiry bounded by all evidence and approval. Each context
must be 32000. Copy complete pricing objects, not a filtered subset: unknown
fields (including overrides or request/cache fees) reject this narrow profile.
Prompt rates must agree, completion must be zero, and discount must be absent or
zero. Input prices are read from metadata, not hardcoded. Future Jev versions
require a reviewed contract update; the requested model remains `jev-latest`.

Remove generic `prompt_per_token`, `completion_per_token`, `cache_read_per_token`,
`cache_write_per_token`, `request_fee`, and `other_fees_upper_usd`. They are not
accepted in this profile. Auxiliary fees are **not applicable** under the
[documented Jev input-only contract](https://openrouter.ai/docs/guides/community/jev),
not guessed numeric zeros. Only the fixed System One model/state/questions
request shape is allowed. No ledger, credential, consent-hash or transport field
migration is needed; refresh existing grants/catalog expiry as usual.

The full-context ceiling is `32000 * prompt`, currently `$0.001344` per call.
Gate/context operations reserve their entire authorized cap before execution;
each subcall independently requires a fresh quote that fits the remaining
reservation before any HTTP request. Three single-call smoke checks therefore
need at least `$0.004032` reserved in aggregate under the published tariff.
Context batching/refinement can require more calls; cap the operation accordingly.
Actual billed cost is reconciled, and missing cost, overbilling or response
provider/model mismatch halts the ledger. A moving alias can change after
preflight; detecting a changed response prevents subsequent calls, not the
already-issued call. Local bounds rely on the provider honoring published prices.

## Credential and transport boundary

Direct mode consumes only the **running child process's** `JEV_OPENROUTER_TOKEN`,
provided by the authorized host injector. Agent tooling must not read or print
that variable. Runtime validates the OpenRouter token shape before use and rejects
literal `op://` references and proxy/placeholder strings. Tokens remain in process
memory/HTTPS headers only; they are never returned in tools, logs, receipts or
config. A malformed/missing injection yields `credential_unavailable`.

Direct mode uses ordinary verified TLS to fixed
`https://openrouter.ai/api/v1/systemone`, requested model `jev-latest`, without
implicit environment proxies, redirects, retries, or alternate providers. It is
not a workaround for network policy: if the local host disallows direct HTTPS,
stop and resolve authorized access. Do not switch modes after a failure.

Proxy mode preserves the existing cloud contract: `OPENROUTER_API` is an issued
placeholder used only through the explicit enforcing HTTPS proxy. It does not
read the Mac token variable. The cloud mode has not been changed or activated.

The provider in a valid OpenRouter response is `TypeSafe`; that identity check
does not permit a direct TypeSafe API call. Vercel Gateway and direct TypeSafe
fallbacks do not exist in this adapter.

## Verification and remaining boundaries

Offline tests use fake token values, fake model I/O and temporary ledgers. A real
STDIO subprocess handshake lists exactly the three tools without model calls.
A passing handshake does not establish 1Password authentication, live OpenRouter
access, valid real pricing, Mac host controls, ledger ownership, or dot access.

The Mac native interface reportedly offers `create_thread`/`send_message` model
and thinking controls; it is not callable from this STDIO process. The parent
must translate only supported observed controls and explicitly dispatch outside
the server. A current local MCP connection does not establish tool propagation
to a dot task: inspect that task's tool inventory and connection separately.
There is no universal automatic routing claim.
