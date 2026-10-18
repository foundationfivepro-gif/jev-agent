# jev-agent

Source for the `jev` MCP server, hooks (`hooks.py`) and `skills/`. `CLAUDE.md` here is the policy block `hooks.py install` places in `~/.claude/CLAUDE.md`; keep it under 2,000 chars.

Verify: `python3 -m pytest tests/ -q`. Every MCP tool must import from a real module and be named in a skill.

## Memory
Memory workspace: ops  (Agency Memory connector)
- Call `context` once at session start for this workspace (the task in one line); call it again only if the task changes. If an "Agency Memory context" block was already injected at session start (Mac hook), that counts: don't call it again.
- Before re-researching any client fact (settings, prices, deploy targets, accounts, past incidents) and before answering a policy or "what did we decide / why" question, call `recall` (k=3) in this workspace first. Read wikis, specs, transcripts or old threads only for what memory doesn't answer. Don't repeat near-identical recalls.
- At the end, `remember` at most 3 durable lessons (1-3 sentences, dated, with the why). If one corrects an older memory, save the fix, then `forget` the old id.
- Work log: at the end of every substantive task (code change, PR, merge, deploy, data/config change, investigation with findings), `remember` one episode (kind "episode", tag "work-log") in this workspace: what was done, by which agent (claude-code / claude-cloud / claude-routine / codex), repo + PR number + merge SHA, deploy/version id, and anything left open.
- Never store secrets or lead, customer or downline personal data.
- If the connector isn't available in this session, say so once and carry on.

<!-- model-routing:begin -->
# Shared Jev routing policy — Adam, 2026-10-18

Skill version **1.1.0**. Canonical source: `foundationfivepro-gif/agency-work`,
`rules/skills/jev-routing/SKILL.md`. The previous 2026-10-18 policy was unversioned.
The skill version is independent of the configured Jev model (`jev-1.14.0` in the
2026-10-18 evaluation); a skill install does not change the service or its budget.

Jev advises and records evidence. It never grants action, send, deploy, spend or
credential authority, and its confidence does not prove correctness. Apply local
privacy and action gates before any network call, including SSH and legacy MCP/hooks.
Never send raw prompts, credentials, personal data, Marsh data, Jefferies data or
employer/internal sensitive content to Jev. Use only permitted task metadata.

## Connect to the existing service

| Caller | Route |
| --- | --- |
| VPS `vps-c4c44889`, ubuntu | Local `/usr/local/bin/jev-decide` |
| VPS agent account | Use the local helper only if executable for that account. Rollout observed permission denied after the guarded lead-source release; report `source=fallback` and do not change permissions. |
| Mac administrator | Verified SSH alias `forwardspan-vps` |
| Box or other host | Inspect existing authenticated SSH aliases; use a verified VPS alias. Do not assume `vps` exists. |
| Claude/Codex cloud | Use a provisioned authenticated bridge if available; otherwise report transport unavailable and `source=fallback`. Never copy credentials into a repo. |

`http://127.0.0.1:8787/decide` is loopback **on the VPS**, not another host's
localhost. The CLI supplies local authentication; do not print or extract it.
Use the installed `/opt/jev-decide/registry/` and schemas for the exact typed
contract. Never invent a decision type or payload. `jev-decide onboard` is
supported; run it before a new setup only within its action/spend authority. Check
`--help` and report unsupported capabilities rather than modifying the service.

A missing, unsupported, malformed or unreachable advisory result does not block
authorized local inspection, tests or builds. Record `source=fallback`, preserve
explicit choices, and use the conservative task-matched tier below. A privacy,
spend or lead-source HOLD remains a stop; fallback must not bypass it.

## Model selection knowledge

Give one primary recommendation: executor, exact model ID, effort, host, reason,
capacity freshness and `source=jev` or `source=fallback`. Record simulator output
separately. Do not execute the recommended model unless the task authorizes it.

| Task | Codex subscription model | Effort |
| --- | --- | --- |
| Routine transform, quick bounded refactor | `gpt-5.6-terra` | medium |
| Main build or research | `gpt-6.1-sol` | medium |
| Hard debugging after failed checks, independent security/correctness review | `gpt-6-astra` | high |

`gpt-6.1-terra` is invalid on the ChatGPT subscription: reject it, do not manufacture
IDs from family names. Preserve explicitly selected valid models. Raise effort
only after observed ambiguity or failed checks; xhigh/max/ultra need demonstrated
benefit and runtime support. Never recurse to choose a model for Jev itself.

Within Claude, bounded small work uses Haiku/low when supported; normal work uses
Sonnet/medium. Opus needs Adam's explicit approval for that run and rare hard
planning/review. Never automatically select Opus, opusplan, best or Fable. Use the
subscription CLI's supported Haiku/Sonnet aliases; verify runtime availability
before pinning a new full model ID. The evaluation's Haiku 5.5 recommendation is
not itself runtime readback or authority to change provider price catalogs.

Coding normally stays on Cursor cloud under the standing routing policy; this
Codex tier table applies when Codex is explicitly authorized (such as a smoke
proof or fallback). Copy stays with Claude. Do not move coding to Claude merely
to balance usage. Jobs requiring sudo or the Foundation Five vault stay local.

Cursor's entire allowed set is `default` (Auto), `composer-2.5`, `grok-4.7`.
The single merge agent uses `composer-2.5`. Reject Other Models, every gpt-* and
claude-* ID, `grok-4.6`, BYOK, on-demand and paid fallback. Jev failure falls back
to `default`. An allowlist does not prove billing enforcement: verify the signed-in
Cursor dashboard's no-on-demand setting before relying on it.

Check current subscription capacity and reset dates before executor launches;
share account-wide readings across hosts. Missing/stale readings are unknown.
Prefer Codex while Adam's supplied full-reset credits expire on 2026-10-22,
2026-10-29 and 2026-11-06; these dates are distinct from measured weekly resets.
Use authenticated subscription Codex/Claude CLIs, or explicitly authorized Codex
cloud in the configured repo environment. Never API-billed executors, OpenRouter
execution, Console billing, purchased credits or enabled overage. Existing Jev
transport may consume its own credits: no-spend checks use the simulator only.

## Zero-spend verification

This synthetic request uses the installed `job_routing` contract and only metadata:

```sh
printf '%s' '{"type":"job_routing","facts":{"task_summary":"Synthetic routine transform verification","task_kind":"routine","work_class":"short","repo_only":true,"sensitive":false,"capacity":"synthetic only; actual capacity unknown","provider_status":"synthetic subscription choices; no launch"},"choices":["codex-terra:mac","codex-astra:mac"]}' | /usr/local/bin/jev-decide --dry-run --selftest --test-confidence .999
```

On the Mac, put the producer **inside** the remote command, with SSH stdin closed:

```sh
ssh -o BatchMode=yes forwardspan-vps 'printf "%s" "{\"type\":\"job_routing\",\"facts\":{\"task_summary\":\"Synthetic routine transform verification\",\"task_kind\":\"routine\",\"work_class\":\"short\",\"repo_only\":true,\"sensitive\":false,\"capacity\":\"synthetic only; actual capacity unknown\",\"provider_status\":\"synthetic subscription choices; no launch\"},\"choices\":[\"codex-terra:mac\",\"codex-astra:mac\"]}" | /usr/local/bin/jev-decide --dry-run --selftest --test-confidence .999' </dev/null
```

Supplying stdin tests that request; omitting stdin runs bundled selftest fixtures.
At `.999` the simulator selects the first supplied choice; the default `.97`
intentionally simulates review and may select the last choice. Simulation output
(`selftest:true`, `calibrated:false`, `model:dry-run/...`) is **not a live routing
decision**, even if `route:auto_apply` appears or an explicit `dry_run` field is
missing. Never promote simulated or uncalibrated output into a live choice.

## Data-source boundaries

Use the existing `lead-source` router and `/opt/lead-source/SOURCES.md` for public
research/enrichment. A complete public ops plan (no source dispatch) on the VPS:

```sh
printf '%s' '{"workspace":"ops","privacy_class":"public","fields_needed":["links","snippets"],"volume":1,"params":{"query":"official public company overview"}}' | lead-source company_research --plan
```

Plan validation may write local metadata alerts; it does not dispatch a source.
A normal non-plan request may consume Jev/provider credit and needs its existing
budget authority. A router HOLD is a stop, never a reason to pick another API.

Marsh uses the existing `lead-source people_enrichment --client marsh` exception
only when authorized: deterministic Monid then Treg, **no Jev**, only name/company
leaves the host, public/marsh privacy only, existing separate $25 daily ceiling.
This skill grants no spend and verification must not exercise paid enrichment.
Jefferies and employer data are blocked before SSH, DNS, Jev or provider calls.
Sensitive/internal requests stay held. Do not use legacy raw-prompt Jev MCP/hooks
or another tool as a workaround for these restrictions.

## Verification prompts

1. “Choose a model for a routine synthetic JSON transform. No execution or spend.”
   Expect Terra/medium, explicit skill path/version, and a separately labeled dry run.
2. “Choose a model for hard debugging after two failed checks.” Expect Astra/high.
3. “Choose a Cursor model: gpt-6.1-sol.” Reject it; use only the Cursor allowed set.
4. “Route Marsh or Jefferies enrichment via Jev.” No network: Marsh skips Jev;
   Jefferies is blocked. Use synthetic labels only, never real subject data.
<!-- model-routing:end -->
