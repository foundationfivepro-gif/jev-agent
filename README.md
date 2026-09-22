# jev-agent

Jev decision modules, built on the official `typesafe-sdk`. Jev is the **only**
external model — every other step is deterministic code.

Transport is the **Vercel AI Gateway** (`typesafe-ai/jev`). That is the only
transport — see "One transport, on purpose" below.

```bash
pip install -r requirements.txt
cp .env.example .env      # set AI_GATEWAY_API_KEY
python -m pytest tests/ -q
./install.sh all          # skills + hooks + policy + MCP registration for Claude Code and Codex
```

## Deploying to Claude Code and Codex

Two artifacts with different reach. Do not confuse them.

| | skills / rules | MCP server (`mcp_server.py`) |
|---|---|---|
| Claude Code desktop + CLI | `SKILL.md` → `~/.claude/skills` | yes |
| Codex CLI | `SKILL.md` → `~/.agents/skills` (same files) | yes |
| Cursor | `.mdc` → `<repo>/.cursor/rules` (converted, per repo) | yes |
| Grok CLI | **already reads `~/.claude/skills`** via `[compat.claude]` | yes — `[mcp_servers.jev]` in `~/.grok/config.toml` |
| Claude mobile | only if saved to your **account** | no (local process) |
| needs a key | no | yes |
| what it does | makes the agent decide better | does the deciding, and saves the tokens |

**Skills.** Codex and Claude use the same `SKILL.md` contract — `name` + `description`
frontmatter — so one set of files serves both. `install.sh skills` copies them to
`~/.claude/skills` and `~/.agents/skills`. They load when their description matches.
Files on disk do **not** sync to mobile; only skills saved to your Claude account do.

**MCP server.** A local stdio process exposing eight read-only tools — six for
coding agents, two for automation harnesses. It decides; it never edits files or
runs commands.

| tool | use |
|---|---|
| `jev_select_context` | **before reading files** — 508k → 3.9k tokens on a 188-file repo |
| `jev_gate_command` | before running any shell command |
| `jev_classify_data` | before sending file contents anywhere; local-only, no model call |
| `jev_evaluate` | arbitrary typed decisions |
| `jev_file_outline` | exported symbols without loading the file; local-only |
| `jev_route_model` | before spawning a subagent — cheapest Claude model that should pass; `selected` goes straight into the Agent tool's `model`. Fable is the top of the catalog at 2× Opus, never the default |
| `jev_should_run` | before a scheduled automation executes — skip runs that would find nothing |
| `jev_check_action` | before any action with external effect, against plain-English policy |

`./install.sh mcp` prints the exact registration for Claude Code and Codex.

**Hooks (Claude Code only).** The tools are advisory — the agent calls them when it
thinks to. `hooks.py` makes three of them events Claude Code runs on its own:

| event | hook | what runs |
|---|---|---|
| `UserPromptSubmit` | `prompt` | one Jev call per prompt: complexity and whether repository context is needed, injected as a one-line note |
| `PreToolUse` on `Bash` | `gate-bash` | `jev_gate_command`; a hard block is deterministic and needs no key |
| `PreToolUse` on `Agent\|Task` | `route-agent` | `jev_route_model`; sets `model` on a subagent that did not choose one |

`./install.sh hooks` merges them into `~/.claude/settings.json` (user scope, so every
session), places the defaults policy (`CLAUDE.md`) in `~/.claude/CLAUDE.md` and
`~/.codex/AGENTS.md`, and registers the server in `~/.codex/config.toml` when Codex is
present. Codex and Cursor have no equivalent hook surface; there the policy file and the
skills are the mechanism, and `jev_route_model` is a decision the agent reports rather
than one the harness applies.

Because a hook runs on every event, it is built around three rules:

- **A hook never bypasses your own permission rules.** On `allow` it says nothing. An
  unreachable Jev, or one that has not answered within 20 seconds, becomes `ask` — a
  hook that outlives Claude Code's timeout is killed and the call proceeds ungated, so
  the deadline is answered explicitly, not waited out.
- **`deny` is reserved for the irreversible**, because the user cannot override it:
  the dangerous-construct patterns and `rm` aimed at root, home or a wildcard. A plain
  `rm -f build/tmp.o`, or a line `shlex` cannot parse, is `ask` — the MCP tool's
  advisory hard block would deny both, and a gate that denies routine commands is a
  gate that gets switched off.
- **Prompt text and command lines leave the machine** — that is what a judgement
  costs. Anything credential-shaped is classified locally, held back, and turned into
  `ask` without being sent.

**Cursor** differs twice, so it has its own installer. It does not read
`SKILL.md` — its equivalent is `.cursor/rules/*.mdc`, different frontmatter — and
those rules are **project-scoped**, not global, so they install per repository
while the MCP config installs once.

```bash
./install.sh cursor ~/code/myproject        # rules + mcp.json
python3 install_cursor.py --repo ~/code/x --dry-run
python3 install_cursor.py --mcp-only        # server only
```

The rules convert to *agent-selected* mode (`alwaysApply: false`), which is the
closest analogue to how a skill behaves — Cursor pulls one in when its
description matches. `context-tiering` and `jev-evaluation` also carry `globs` so
they auto-attach when relevant source files are in context.

The `mcp.json` write is a **merge**: existing servers are preserved, a timestamped
backup is written first, and an unparseable file is refused rather than
overwritten. Cursor does not expand shell variables in `mcp.json`, so pass
`--key` or point `env` at an `envFile`.

## Universal reach: local vs remote

Three mechanisms, and only one reaches a phone.

| | skills → your Claude **account** | local MCP | remote MCP connector |
|---|---|---|---|
| Claude mobile | **yes** | no | yes |
| claude.ai web | **yes** | no | yes |
| Claude desktop / Code | yes | yes | yes |
| setup | save the skill cards | `./install.sh all` | deploy + Customize → Connectors |

Skills reach every surface but only change how the agent *decides*. The local
MCP server does the deciding and produces the token saving. The remote connector
carries a subset of the tools to surfaces where no local process can run.

### What can and cannot go remote

`remote_server.py` exposes five tools. Two are deliberately absent and one is
deliberately reduced:

| tool | remote | why |
|---|---|---|
| `jev_evaluate`, `jev_should_run`, `jev_check_action`, `jev_gate_command` | yes | pure logic, judge what you pass them |
| `jev_select_context` | **no** | its saving comes from reading *your* repository; a remote version would have to upload the codebase to answer the same question |
| `jev_file_outline` | **no** | same reason |
| `jev_classify_data` → `jev_classify_paths` | reduced | the local version scans file **content** and never transmits it. A remote content scanner requires uploading the material it exists to protect — worse than none, because it is trusted. The remote variant takes paths only, and says so in its output |

So the largest token saving is inherently local. That is a property of the
problem, not a gap.

### Deploying

```bash
vercel --prod                                    # vercel.json + api/index.py included
# Vercel → Settings → Environment Variables:
#   AI_GATEWAY_API_KEY = vck_...
#   JEV_REMOTE_TOKEN   = a long random string
```

Then in Claude: **Customize → Connectors → Add custom connector**, URL
`https://<host>/mcp`, authentication **No sign-in**, and under **Request
headers** set `authorization` to `Bearer <your JEV_REMOTE_TOKEN>`.

If your organisation lacks the request-headers beta, use the URL form
`https://<host>/t/<token>/mcp` instead. That puts a credential in a URL, where
it lands in logs and history — prefer the header, and treat a path token as
disposable.

`GET /health` is unauthenticated and reports whether the key and token are set.
**With no `JEV_REMOTE_TOKEN` the server refuses every request** rather than
serving an open endpoint that spends your Jev quota.

## One transport, on purpose

A direct `api.typesafe.ai` path was written and removed. With no TypeSafe key to
exercise it, it would have been untested code reached only when something had
already gone wrong — the worst kind to ship.

`typesafe-sdk` remains a dependency for its question *types* only. They are
pydantic models that reject a malformed `criteria` before any network call — a
Score handed a string, a Choice handed a list — which is the single easiest
mistake to make with this API. The SDK's client is unused.

Because the gateway is the production transport, every number below was measured
on the path that will actually run. They are not estimates carried over from a
different endpoint.

## Modules

All ten systems from the engineering guide, plus the runtime they share.

| file | decision | model? |
|---|---|---|
| `core.py` | gateway transport, size-aware batching, traces | — |
| `canary.py` | detects Jev's silent failure mode | — |
| `symbols.py` | exported symbols via `ast` / ast-grep | no — parser |
| `permission_gate.py` | allow / review / block one command | Jev |
| `security_router.py` | classify data, pick a permitted provider | Jev |
| `tool_router.py` | select one tool, or none | Jev |
| `context_tier.py` | include / index / exclude per chunk | Jev |
| `compaction.py` | full / index / drop per history chunk | Jev |
| `model_router.py` | pick the executor model | Jev |
| `subagents.py` | smallest sufficient read-only worker plan | Jev |
| `conditional_agents.py` | which repo rules constrain this task | Jev |
| `background_review.py` | which read-only reviewers to run | Jev |
| `control_loop.py` | assemble and gate the execution packet | — |
| `hooks.py` | Claude Code hook adapters: prompt evaluation, Bash gate, subagent routing | Jev |

`python -m pytest tests/ -q` — 81 tests, 74 of which need no key. Two of them are
integration guards: every MCP tool must import from a real module, and every tool
must be named in a skill. The hook tests run `hooks.py` as a subprocess with no key
and an absent env file, so they prove the hard block and the fail-silent paths
without touching the network. A capability no skill describes is one the agent never
thinks to call, which is the difference between code being *in* the repo and
being *merged* into it.

## Harness variant (Grok Bot and similar)

`harness.py` carries the patterns for an automation harness rather than a coding
agent. The economics differ: a coding agent is one long session with a large
context, so the win is reducing context per task; a harness is many short runs at
high frequency, so the win is **not running at all**.

| | `should_run()` | `check_action()` |
|---|---|---|
| decides | does this scheduled execution need to proceed | does a proposed action satisfy policy |
| fails | **open** — when unsure, run | **closed** — when unsure, review |

For a weekday job where ~70% of runs find nothing, ~180 full pipelines a year are
avoided for roughly a cent of gating.

`check_action` generalises a hand-maintained allow/block list. A literal list of
sentences only fires on the wording someone anticipated; handing the same
sentences to Jev as *criteria* lets near-matches resolve while the policy stays
readable. Block is evaluated first and always wins.

Two things learned building it, both worth copying:

**Word the question around the harm, not around deviation.** An earlier anomaly
question asked about "a spike, a long gap, or a pattern unlike a routine run" —
which made four consecutive quiet runs read as anomalous. Quiet is the normal
state of a recurring job.

**Give a gate a baseline.** "Is this far larger than normal" is unanswerable
without knowing normal. Adding `typical_new_files_per_run` moved the anomaly
score on a 4000-file catch-up run from 0.73 to 0.86 — across the escalation
threshold. The anomaly question is also asked in its **own call**, because the
same question scored 0.73 batched and 0.86 alone: a safety judgement should not
depend on what else was in the batch.

## Three things that differ from the engineering guide

**1. Question ids do not bind to state keys.** Every question is evaluated
against the *whole* state. A question with id `f3` has no implicit link to
`state.chunks.f3` — it must say so in its instructions. Getting this wrong
returns confident, uniform, wrong answers **with no error**; measured separation
between relevant and irrelevant items collapsed from 1.91 to 0.01.

Every fan-out here runs known-answer canaries and raises if they fail to
separate. `tests/test_all.py::test_canary_catches_broken_binding` reproduces the
bug deliberately and asserts the detector fires.

**2. The per-call ceiling is payload BYTES, not question count.** Question count
is what gets tuned (`MAX_QUESTIONS=50`; ~150 works in isolation, 170+ hard-fails),
but size is what actually binds. Enriching each item's state — adding real
signatures from ast-grep — made batches of 50 start failing that had worked at 50
before, with no change in count. `decide_batched` therefore packs to
`MAX_PAYLOAD_CHARS` as well as to a count.

It also takes a **callable** for state, so each batch carries only the items its
own questions ask about. A fixed state is re-sent in full with every batch — a
188-item fan-out sent the whole corpus four times and 503'd.

**2b. Scores are only comparable within a batch.** Each question is told to judge
one item, but its batch-mates are still in state and shift the answer: the same
corpus split into smaller batches moved recall from 7/8 to 6/8 with nothing else
changed. `context_tier.select` runs a second pass over the top candidates in a
single call so boundary decisions are made on common footing. Ranking across
batches without that compares numbers produced under different conditions.

**3. "Summarize" is not a context tier.** The guide's compaction offers
full / summary / drop. Tested with the original source visible to the judge:

| check | result |
|---|---|
| could a developer modify this from the summary alone | **0.41 – 0.47, fails** |
| does it point at the symbols they need | **0.77 – 0.94, passes** |

Both a cheap model and a careful hand-written summary failed substitution, so a
better summarizer is not the fix. For small files the summary came out at
132–320% of the original — larger than the file it replaced. So the middle tier
is **index** (path, doc line, exported symbols, from a parser) and nothing under
`MIN_INDEX_TOKENS` is ever indexed.

This also satisfies the Jev-only constraint: no generative model is needed
anywhere in the loop.

## Two security fixes

The guide's versions fail **open** on the cases they exist to catch.

Its secret regex requires a keyword followed by `:` or `=`, catching 1 of 6
realistic formats — it misses the PEM block its own verification step tells you
to test with. `security_router.classify` now covers PEM/OpenSSH/PGP armour,
provider key prefixes, JWTs, connection strings, header forms and high-entropy
blobs, and fails closed. 15/15 on the test corpus.

Its hard block tests `argv[0]`, so `rm -rf /` is blocked but `/bin/rm -rf /`,
`sudo rm -rf /`, `bash -c 'rm -rf /'` and `find . -delete` all pass.
`permission_gate.extract_commands` resolves paths, follows wrappers, recurses
into `sh -c`, and splits pipelines. The block list is deliberately narrow —
irreversible only — because false positives are how a gate gets switched off.

## Measured

188 TypeScript files, 195k tokens, goal "find where JWT is verified and expiry
handled":

```
recall@8: 6/8
4 included, 3 indexed, 182 excluded | 3,947/195,103 tokens (98% saved)
canary: ok (separation 0.70)
elapsed: 4.6s
```

Read that recall figure with care — the ground truth is "path contains jwt",
which is crude. The two misses are `jwa.ts` (algorithm name constants) and
`utf8.ts` (a text-encoding helper); the two false positives are `jwk/jwk.ts`
(JSON Web Key, used *for* JWT verification) and `bearer-auth` (token extraction).
For the stated goal the ranking is arguably better than the labels, which is a
reason to build a real labelled set before tuning against this number.

An earlier run scored 7/8 by sending all 188 items with full state in two large
batches. That is more comparable and less reliable — it 503s as state grows. The
tradeoff is real: bigger batches calibrate better, smaller batches survive.

## Caveats

`symbols.py` parses Python with `ast` and TypeScript with regex. Use `ast-grep`
or `tree-sitter` for the latter in production.

The substitution finding rests on two files judged by one evaluator. It is
strong enough to stop you building a summarize tier for code, and worth
re-testing before applying it to prose, where compression may behave differently
— prose restates, code specifies.

Six of the ten systems in the engineering guide are not built here: tool router,
compaction, model router, subagents, conditional AGENTS.md, structured skills and
background review. They are straightforward on this base now that `core.py`,
tracing and canaries exist.
