---
name: codex-jev-routing
description: Require JEV selection before each adapter-controlled model execution in Codex, preserving explicit model constraints and host authorization, with a named direct-local non-model exception.
---

# Codex: JEV-first model execution

For each new model execution request controlled by this integration, obtain a
JEV decision before execution. This includes clear tasks, writing, delegated
model work and explicit user model choices. Explicit choices narrow JEV's
eligible catalog; they do not bypass the routing record. Never override them.

## Required flow

1. Establish the exact runtime's supported model IDs, efforts and tools. Parent
   access does not prove child access. Keep API, native, UI and OpenRouter IDs
   distinct. Screen/minimize context and check host permission, consent, deadline
   and the total routing-plus-generation budget first.
2. Invoke the trusted JEV selector once for the model request. Record request ID,
   decision ID, source/provenance, eligible choice and routing usage/cost. The JEV
   control call itself uses the established JEV route directly; never call JEV
   recursively to choose a model for JEV. Reentrant selection blocks.
3. Validate the decision against the same explicit choices, catalog, supported
   effort/tools, provider/privacy rules and budget. A decision is not permission.
4. Execute only through an independently authorized, enabled host adapter. Bind
   the receipt to that request/decision. An idempotent replay is not a new model
   request. A safe retry of the unchanged request retains its decision; changed
   task/model/provider constraints need a new validated routing decision.

Missing JEV access, outage, malformed selection or unsupported host controls
blocks the model request. Report the specific unavailable capability. Never
substitute a deterministic/default/other-provider model to keep going.

## Direct local execution exception

Non-model work that requires local execution may proceed through normal host
permissions without a JEV model-selection call. Record the named operation and
a sanitized reason: local_file_io, local_test, local_build, or
local_environment_inspection. This record never grants permission. Local model
inference is still a model call and cannot use this exception. The model adapters
do not accept local exception receipts as routing decisions.

## Integration surfaces and limits

- `jev_recommend_host_route` requires a trusted selector binding. Tool-supplied
  capability snapshots cannot attest runtime provenance or fabricate JEV proof.
- `jev_plan_writing` requires JEV selection for every new plan, including explicit
  Sonnet/Opus choices. `jev_generate_writing` requires that recorded plan decision
  and returns the draft unchanged; do not automatically rewrite it.
- `jev_select_context`, `jev_file_outline`, `jev_gate_command`,
  `jev_classify_data` and `jev_check_action` retain their safety roles. A scanner
  result or model verdict cannot grant authority. Local tools require approved
  roots and host permission.

This skill cannot intercept a platform-fixed current chat model or every hidden
platform model call. Only a host-exposed adapter can enforce a route. Do not claim
global activation, change the current chat model, or invent a hook. Disclose
unsupported surfaces instead. Installing this file does not install a server,
grant access, deploy code or enable live dispatch.

Existing JEV upstream: OpenRouter `/api/v1/systemone`, model `jev-latest`.
Client OAuth and secure proxy binding names remain separate. Never read or copy
keys. All new dispatch/provider defaults remain disabled until separately enabled.

## Efficient coordination without bypassing JEV

Where the host actually offers the corresponding native IDs, present Sol as a
normal coordination candidate, Luna for narrow verified work, Codex workers for
substantial code inspection/changes, and Astra for difficult architecture or
evidence-driven escalation after failed acceptance checks. Latest Sonnet/Opus
remain writing-family candidates. These are preferences in the eligible catalog,
not direct picks or promises that a fixed parent model can switch. JEV still
selects for every new adapter-controlled model request.

Keep context already held by the coordinator inline when appropriate. Delegate
when a worker can inspect much more than it returns. Send a bounded task,
constraints, relevant paths/references and acceptance checks. Require a compact
receipt: result, changed files, tests, blockers, routing decision ID, model/effort,
usage/cost (unknown explicitly), and evidence links. Do not reread full worker
transcripts or bulk source without a concrete verification need. Compare the
total parent + worker + JEV + retry/cache/tool cost and tokens per accepted task;
moving usage to another provider is not savings.

Deployment status for this source branch: not installed or activated in web,
the user's Mac, or dot. Each exact host must independently expose and verify the
adapter/catalog before any support claim. Publication alone changes no runtime.

## Verification

Run `python scripts/offline_tests.py` with real dependencies. Acceptance includes
JEV records for every eligible call, explicit-choice preservation, unavailable
fail-closed behavior, sanitized local exceptions, non-recursion, idempotency,
routing-inclusive budget accounting and no installed Claude/config changes.
