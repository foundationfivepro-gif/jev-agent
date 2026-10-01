> Live host-routing code is now available through explicit dependency injection; see
> [LIVE_ROUTING.md](LIVE_ROUTING.md) for setup, verified coverage and remaining gates.
> Historical mock/pilot claims below do not constitute activation evidence.

# JEV offline portability milestone

## Current routing policy (1 October 2026)

The Codex integration now requires a recorded JEV selection for every new
adapter-controlled model execution, including clear writing and explicit model
choices. Explicit choices constrain selection; they do not bypass it. Missing or
invalid JEV selection blocks rather than choosing a default. The sole named
exception is direct local **non-model** work under host permission. Internal JEV
control calls are never recursively routed. See
[Codex routing skill](.agents/skills/codex-jev-routing/SKILL.md).

This supersedes the earlier selective-routing proposal. It does not intercept
the platform-fixed current chat model, install host adapters, change installed
Claude hooks, or establish global activation. New selectors remain explicit
trusted bindings; in-process mock evidence is labeled synthetic.


## Scope and provenance

Prepared in an isolated Linux VM copy of private repository
`foundationfivepro-gif/jev-agent`. Reviewed upstream commit:
`e7a002e1eb8321c5ef8ea4de8a921a924faf5cc6`; main matched that commit when
read through the authorized connector. All 50 fetched blobs were verified against
their Git object hashes and executable modes. The exact baseline tree is
`9bad7be30e6346b03bcc237790eb13a9c3e14994`.

This is local code preparation and synthetic testing. Nothing was pushed,
deployed, installed into a host, or written into the user's Mac/Claude settings.
No secrets were read and no provider calls were made for this milestone.

## What changed

- Screen decision state/questions before request serialization and retry storage;
  sanitize traces, filenames and transport errors. Refuse redirects so an
  authenticated decision request cannot silently change destinations.
- Default context selection uses screened paths and symbol identifiers without
  source snippets, docstrings, literal defaults or arbitrary fallback text.
  Explicit internal snippet opt-in remains screened and separately scoped.
- Local reads require independently configured `JEV_WORKSPACE_ROOTS`. Root
  traversal uses descriptor-relative opens with no symlink fallback. Sensitive
  targets, hardlinks, nonregular/binary/growing/oversized files fail closed.
- Hook review/block/error/unavailable outcomes request approval; deterministic
  irreversible denials remain. Opaque interpreter/script commands ask without
  sending their content. No model result or rewritten Agent input grants host
  permission. Hooks are not an OS reference monitor; arbitrary programs/build
  tools still require the host's sandbox and authorization.
- Local/remote command schemas share versioned `operator_allowlist` support.
  Empty model menus remain empty, with typed failure carried through MCP.
- Strict host contracts distinguish native executor IDs, API IDs, UI labels and
  OpenRouter IDs. Exact runtime/session observations, expiry, effort, tools,
  explicit choices and host override limits gate recommendations.
- Add `jev_plan_writing`, `jev_generate_writing` and
  `jev_recommend_host_route` identically on both servers. Local inventory is
  12 tools; remote inventory is 10. Remote filesystem access is still absent.
- Mock writing uses official latest-family alias strings, stable metadata,
  explicit provider/privacy constraints, scoped synthetic authentication,
  per-tenant reservations, idempotency and attributable receipts. Draft bytes
  are returned unchanged. Refusals never trigger a broader fallback.
- SDK argument/runtime errors receive fixed, non-echoing diagnostics before
  server logging. Credential-in-URL paths/queries are rejected.

## Important intentional compatibility changes

1. Configure an approved workspace root before local context/outline tools work.
   Request parameters cannot grant new roots. Internal symlinks are also rejected.
2. Previously warning-only consequential hook outcomes now ask for approval, even
   in bypass mode. Opaque interpreter/script execution is conservative and may
   ask for benign scripts; this is an intentional enforcement tradeoff.
3. Source snippets are no longer implicitly submitted. Symbol-only usefulness is
   tested on synthetic fixtures, not claimed equivalent on all real repositories.
4. Inline source allowlist comments cannot authorize external disclosure.
5. Unknown/invalid models and empty filters fail closed rather than widening the
   catalog. New schema fields expose typed failures.
6. The former `/t/<token>/mcp` workaround no longer works. Existing decision
   authentication supports headers; new writing authentication is only a mock
   preparation boundary, not a deployed OAuth issuer.
7. Both requirements files pin MCP 2.2.0, the SDK API actually exercised. The old
   remote `mcp>=1.2` claim did not match its own MCPServer imports.

The existing decision key precedence is unchanged:
`OPENROUTER_API_KEY` → OpenRouter `/api/v1/systemone` with `jev-latest`;
legacy explicit TypeSafe/Gateway configurations retain their existing precedence.
No failed call silently switches upstream. Client auth is a separate boundary.

## Mock-only boundaries and rollback

`writing_service.DEFAULT_SERVICE` is disabled. Only exact concrete in-process
mock dependency types are accepted when a fixture explicitly enables it. The
production provider transport does not exist in this service; no environment
flag can activate one. `dispatch_recommendation` always returns unavailable.
The separate pilot harness is not registered as an MCP tool and cannot enable
service defaults.

Disable the fixture service and stop using the new recommendation wrappers to
roll back new behavior. Preserve all privacy/root/trace fixes. No installer,
`.claude/settings.json`, `.mcp.json`, deployment config, or upstream credential
configuration was changed. Actual deployment rollback/revocation remains unrun.

OAuth metadata, PKCE, grants, revocation and tenant isolation are synthetic,
in-memory preparation. They are not durable, cryptographically signed production
identity. The mock ledger is in-process; live distributed quota/reservation
infrastructure needs separate review. Live credential provisioning, issuer/host
selection, storage/retention, incident ownership and production grants remain
unresolved.

## Verification and reproducibility

Create a disposable environment using `requirements-offline.lock.txt`, then run:

```sh
python scripts/offline_tests.py
python -m compileall -q .
python -m pip check
git diff --check
```

The runner clears credentials/environment configuration, redirects HOME and
traces to disposable directories, skips live-key tests, disables pytest plugin
autoload and inherits socket denial into Python subprocesses. Real pytest,
Pydantic, MCP and symbol-parser packages are used, not replacement SDK stubs.
Dependency installation occurs before the hermetic run; no model calls occur
during it.

The unmodified baseline passed 304 tests and skipped 7 live tests. Consult the
attached test report/JUnit evidence for final expanded counts. In-process MCP
discovery and tool calls are real SDK operations; OAuth/catalog/provider data
and generation are mocks. These layers must not be conflated.

## Surface support and savings

Direct local Codex, delegated local Codex, ChatGPT cloud/web, dot parent,
dot-created executor and installed Claude runtime discovery are **unverified**.
No connected client was installed/granted access or used to run the new flow.
Caller-supplied MCP capability snapshots are explicitly labeled synthetic;
they cannot attest their own runtime provenance.

No measured token, cost, quality or latency improvement is claimed. Offline
ledger values and drafts are synthetic. The separately authorized USD5
OpenRouter pilot must measure paired tasks and every routing/generation/retry/
cache/tool/parent coordination cost, not count moving work to OpenRouter as
savings. API surrogate profiles cannot prove savings in every Codex version or
the actual dot runtime. An actual-host baseline and equivalent acceptance
criteria are required for any host-specific claim.
