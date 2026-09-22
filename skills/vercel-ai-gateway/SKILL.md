---
name: vercel-ai-gateway
description: Call models through the Vercel AI Gateway (ai-gateway.vercel.sh) — auth, model discovery, free-tier gating, reasoning-model token traps, retries. Use when working with an AI_GATEWAY_API_KEY or debugging a gateway call.
---

# Vercel AI Gateway

OpenAI-compatible multi-provider endpoint at `https://ai-gateway.vercel.sh/v1`, auth
`Authorization: Bearer vck_...`.

**Discover models; do not guess ids.** `GET /v1/models` is **public, no auth required**.
Plausible-looking ids 404. Ids are `provider/model`. Check the entry's `type` before choosing
an endpoint — not every listed model is a language model.

**Free-tier gating looks like an auth error but is not.** `Free tier users do not have access
to this model` arrives with an auth-class status. The key is valid; the model is gated. Check
a known-free model before telling anyone their key is broken.

**Reasoning models return empty content on a small budget.** `openai/gpt-oss-*` put hidden
tokens in `message.reasoning`; with low `max_tokens`, `content` is `""` and `finish_reason` is
`length` — which reads as silent failure. Give them ≥500 output tokens and warn on
`!content && finish_reason == 'length'`.

**Errors.** Retryable: 408, 429, 5xx; honour `Retry-After` (free-tier 429s ask for 60s, so a
backoff capped below that never waits long enough). Never retry 401/403.
`providerMetadata.gateway.routing` reports the resolved upstream and fallbacks — read it when
a call behaves oddly.

A project-scoped Vercel MCP connection is often denied `projectEnvVars` (403 on list): you can
usually create env vars but not read them.
