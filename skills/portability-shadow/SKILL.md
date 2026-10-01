---
name: portability-shadow
description: Inspect mock-only JEV host recommendations and latest-family writing contracts without activating live dispatch or generation.
---

# Portability shadow tools

These interfaces are code preparation, not installed-host compatibility proof.

- `jev_recommend_host_route`: pass a TaskEnvelope and the exact runtime/session's observed CapabilitySnapshot. Synthetic observations are labeled synthetic. The result is a recommendation, never permission or execution.
- `jev_plan_writing`: plan a bounded latest Sonnet/Opus mock writing request. A caller-supplied destination can narrow an existing grant, never create one. Planning returns metadata and does not generate prose.
- `jev_generate_writing`: execute a previously prepared mock plan and return the fixture Anthropic draft unchanged with its model/provider/usage receipt. Do not automatically rewrite the returned draft with the host model.

All three are registered consistently on local and remote MCP servers. New writing tools are unavailable by default. Successful tests require explicitly injected in-process mock catalog, auth, budget, transport and JEV-selector objects. Every new model execution requires a recorded JEV decision; no deterministic/direct model fallback remains in the Codex adapters. Direct local non-model exceptions are separately recorded and never authorize a model call. See ../../.agents/skills/codex-jev-routing/SKILL.md. Live dispatch/provider clients are absent. A skill does not install or connect MCP.

## Privacy and consent

Local filesystem tools require operator-configured `JEV_WORKSPACE_ROOTS`. Tool arguments cannot approve a new root. Source contents stay local by default; paths/symbol metadata is minimized and screened. Symlink/hardlink/binary/oversize reads fail closed. Detection is defense in depth, never proof a string is public.

The legacy OpenRouter decision key remains server-side, independent of scoped client authentication. Never put a key or token in prompts, tool fields, URLs, traces, or files. No grant, deployment, paid test, or live call is authorized by these examples. Each connected runtime still needs separate discovery evidence.
