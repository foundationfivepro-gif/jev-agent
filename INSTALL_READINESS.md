# Installation readiness and unapplied patch

One real JEV recommendation and one parent-confirmed dot native worker dispatch
passed. The current parent model did not change. A configuration registration
exposes a tool; automatic enforcement still requires the host to call the bound
route before every controlled worker request. No platform-wide intercept exists.

## Prepared, safe Codex configuration

`config/codex-local-candidate.toml` is an actual disabled stdio MCP configuration
for this workspace. It forwards only named protected-placeholder/proxy variables;
it contains no credential values and does not import the legacy key loader.
The launcher expects `/workspace/operator-jev-binding.py` to define `build_host()`
and return a `BoundHostRouting` using fresh discovery and the canonical ledger.
That operator module is an explicit unresolved dependency, not a fabricated host.

To prepare an additive candidate while preserving an existing configuration:

```sh
/workspace/jev-venv/bin/python scripts/prepare_codex_install.py \
  --existing /absolute/path/to/config.toml \
  --output /absolute/path/to/config.toml.jev-candidate \
  --python /workspace/jev-venv/bin/python \
  --binding /workspace/operator-jev-binding.py
```

Omit `--existing` only for a new candidate. The script never edits the source
configuration, refuses an existing `jev_bound` server, creates the output
exclusively with mode 0600, and never prints existing configuration contents.
Review the candidate, implement the host binding from `LIVE_ROUTING.md`, then
apply it to the intended Codex configuration and enable that one server under
that host's existing authorization. Those last steps have not been performed.
Mac configuration and `.mcp.json` remain unchanged.

Installed CLI observation: `codex-cli 0.159.0-alpha.3`. `codex mcp add --help`
confirms stdio/HTTP registration; `codex mcp login --help` confirms scoped OAuth
login. Configuration fields were checked against the
[official reference](https://learn.chatgpt.com/docs/config-file/config-reference).
No login, server registration, OAuth grant, new hook or deployment was performed.

## Exact proposed remote resource and OAuth fields

The candidate hostname is the existing branch-preview alias, not a new domain:
`jev-agent-git-codex-jev-c1aa85-foundationfivepro-7852s-projects.vercel.app`.

- Resource/audience: `https://jev-agent-git-codex-jev-c1aa85-foundationfivepro-7852s-projects.vercel.app/mcp`.
- Protected-resource metadata: the same origin at `/.well-known/oauth-protected-resource/mcp`.
- Initial scope: `routing:recommend` only.
- Separately requested writing scopes: `writing:plan`, `writing:generate`, `writing:read`.
- Required but unchosen: issuer, authorization/token/JWKS endpoints, accepted
  signing algorithms, client registration mode/ID and exact host callback URIs.
  The JSON design leaves these unset. Never substitute a guessed IdP or redirect.
- Server work: implement resource metadata and challenges, verified bearer-token
  validation, issuer/audience/scope/expiry checks, principal-to-runtime mapping,
  durable revocation, destination/data-class grants, and quotas.

The disabled remote TOML is a reviewable candidate only. The existing shared
`JEV_REMOTE_TOKEN` service is NOT this OAuth resource server, and its writing
authentication is a mock. Do not use No-sign-in/shared-secret registration as a
substitute for this design. No public OAuth route exists yet. Parent is separately
investigating the Vercel collaboration gate; this task makes no team changes.

## Runnable writing dependencies, without activation

`live_writing.py` now supplies `OpenRouterWritingTransport`,
`OpenRouterWritingBinding`, and `OpenRouterWritingCatalog` for the existing
synthetic `LivePilot` dependency interfaces. They implement real fixed-destination
HTTPS chat requests and exact-model catalog metadata reads, with forced proxy,
no redirects/retries/fallbacks, provider restrictions, public-content screening,
usage/cost validation, and unchanged returned draft text. Offline tests replace
only the HTTP boundary. No paid writing call was made for this addition.

Required integration inputs, all supplied by trusted host setup:

1. Exact current Sonnet/Opus alias-to-model mappings; never infer latest from names.
2. Allowed provider and fresh verified ZDR/data-collection/parameter support;
   `privacy_check` must verify these, not merely return true.
3. Verified cache-read, cache-write and request-fee upper bounds where the endpoint
   omits those fields. The catalog reserves full possible input context; a cap may
   reject it. An independently proved smaller input bound would need separate code.
4. Existing protected `OPENROUTER_API` placeholder and HTTPS proxy.
5. The SAME durable `PilotLedger` and approval for routing plus generation;
   `LivePilot` must reserve before entering the low-level transport. Never expose
   `send()` as a public tool or treat it as an authentication boundary.
6. A live writing `JevRouter` implementing the pilot's `prepare/decide` contract,
   including resolved router identity accounting. The new native-worker selector
   does not translate native IDs into Anthropic API IDs.
7. For public writing MCP rather than the synthetic pilot: a production auth
   boundary, durable plans/idempotency and live receipt contracts. The existing
   `WritingService` deliberately continues rejecting these non-mock dependencies.

No new alias mappings, permissions or identities are invented by these adapters.
A low-level runnable transport is not proof that the public writing tools are live.

## Readiness checklist

| Item | Evidence/status |
|---|---|
| JEV live selector, fixed route and protected proxy | Passed; `gen-dec-1790821519-uh7HduqHau0t8AP85YJq` |
| Parent native worker | Passed; `/root/verify_jev_selected_worker`, exact expected JSON |
| Canonical OpenRouter ledger | $0.046548454 spent; $4.953451546 remaining; no reservations |
| Native worker billing | Unknown; do not include it in an invented total/savings claim |
| Codex configuration | Prepared disabled candidate; not applied |
| Per-session discovery/permission/dispatch binding | Operator implementation required |
| Automatic routing enforcement | Host must invoke bound adapter; not globally installed |
| Real writing transport/catalog code | Implemented and offline tested; not paid/live verified |
| Public writing MCP integration | Unfinished; auth/plan/selector/live contracts listed above |
| Remote OAuth | Exact resource/scopes proposed; issuer/client/grants unset |
| Vercel preview | Blocked by collaboration gate; parent investigating |
| Mac, main, Claude config | Unchanged |

## Publication identity

The user clarified that this project must use `foundationfivepro-gif`. The
authenticated GitHub connector profile was independently verified as that
account, with email `foundationfivepro@gmail.com`. Publication of this readiness
change uses that connector, preserving existing commits. Earlier shell commits
used the pre-existing global Git configuration from `/home/agent/.gitconfig`:
`user.name=Adam Savoy`, `user.email=adamsavoy@gmail.com`. This task never set or
changed those values. GitHub resolves those commits to `adamsavoy`. The earlier
successful commit was created through the connector under a different identity.
No identity spoofing, author rewrite, force push, grant or account-setting change
has been attempted. Shell Git authentication was not assumed to match the
connector identity.

Final offline verification: **713 passed, 7 skipped, 82 subtests passed**.
Independent review ran 39 focused tests and found no new blocking safety defect.
Remaining writing/OAuth/host-integration dependencies above are explicit.
