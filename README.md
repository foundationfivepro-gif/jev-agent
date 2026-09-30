# jev-agent

Jev decision modules, built on the official `typesafe-sdk`. Jev is the **only**
external model — every other step is deterministic code.

Transport is **OpenRouter's System One API** (`openrouter.ai/api/v1/systemone`, model
`jev-latest`, key `OPENROUTER_API_KEY`), which serves TypeSafe's own API unchanged.
`TYPESAFE_API_KEY` (api.typesafe.ai directly) and the legacy Vercel AI Gateway are used
only when no OpenRouter key is set — see "Transport" below.

```bash
pip install -r requirements.txt
cp .env.example .env      # set OPENROUTER_API_KEY
python -m pytest tests/ -q
./install.sh all          # skills + TypeSafe plugin + hooks + policy + MCP registration
```

## TypeSafe's official skill and this repo

TypeSafe publishes an agent skill, `typesafe-ai` ([typesafe-ai/skills](https://github.com/typesafe-ai/skills)),
and this repo uses it rather than duplicating it. The split:

| | `typesafe-ai` (TypeSafe's plugin) | `jev-*` skills here |
|---|---|---|
| answers | how to design a judgment: which primitive, what goes in state vs instructions vs criteria, confidence, cookbooks | how *this* runtime calls Jev: transport, `core.decide`, batching ceilings, thresholds, the MCP tools and hooks |
| source of truth | docs.typesafe.ai, read live (`llms.txt` index) | measured numbers in this README and the tests |
| installed by | `.claude/settings.json` for this project; `./install.sh plugin` at user scope | `./install.sh skills` |

`.claude/settings.json` declares the `typesafe-ai` marketplace and enables `typesafe@typesafe-ai`,
so a clone of this repo has it; `install.sh plugin` (part of `all` and of `update.sh`) installs
and updates it at user scope so every other project has it too. `/typesafe:typesafe-ai` loads it
by hand. The `jev-evaluation` skill points at it whenever Jev code is being written, and the
question wording in this repo follows its guidance (backticked state paths such as `` `chunks` ``,
instructions carrying the whole meaning because ids are never sent to the model).

## Install or update on a computer

One command, the same on every computer, in a terminal or in Claude Code (prefix it with
`!`). It finds the existing install from the hooks in `~/.claude/settings.json`, or clones
to `~/jev-agent`, then runs `update.sh`: pull, requirements if missing, skills, hooks,
policy, MCP registration if missing, and a check that the hooks answer.

```bash
d=$(python3 -c 'import json,os;s=json.load(open(os.path.expanduser("~/.claude/settings.json")));print(next(os.path.dirname(a) for g in s["hooks"].values() for x in g for h in x.get("hooks",[]) for a in h.get("args",[]) if a.endswith("/hooks.py")))' 2>/dev/null || echo "$HOME/jev-agent"); [ -d "$d/.git" ] || git clone -q https://github.com/foundationfivepro-gif/jev-agent.git "$d"; git -C "$d" pull --ff-only -q && bash "$d/update.sh"
```

After the first run, `/jev-update` in Claude Code does the same. A new computer also
needs `OPENROUTER_API_KEY` in `<repo>/.env`; without a key only the local parts run. From
1Password:

```bash
printf 'OPENROUTER_API_KEY=%s\n' "$(op read 'op://Foundation Five/OpenRouter API Key - Claude/credential')" >> .env
```

The hooks and the MCP server both read that file, and a key in `.env` outranks nothing
already set in the environment, so an older `--env` registration keeps its key. A
registration holding a *lower* key (TypeSafe or gateway) still switches to OpenRouter,
because the server fills `OPENROUTER_API_KEY` from `.env` and OpenRouter comes first.

## Deploying to Claude Code

This repository targets Claude Code only. Everything here is tuned for one goal: the
fewest tokens in Claude's context for the same result.

| | skills | hooks | MCP server (`mcp_server.py`) |
|---|---|---|---|
| installs to | `~/.claude/skills` | `~/.claude/settings.json` + `~/.claude/CLAUDE.md` | `claude mcp add jev ...` |
| needs a key | no | for the Jev calls; the hard block and return contract need none | yes |
| runs | when a description matches | on every prompt, command and subagent | when Claude calls a tool |

Files on disk do **not** sync to Claude mobile; only skills saved to your Claude account do.

### Where the tokens are saved

| lever | mechanism | saving |
|---|---|---|
| read less | `jev_select_context` before reading files | 508k → 3.9k tokens on a 188-file repo |
| short subagent returns | `route-agent` appends a return contract to every subagent prompt: conclusion only, file:line references, under 250 words | the parent reads, and keeps in context for the rest of the session, a conclusion instead of a transcript |
| cheapest sufficient model | `route-agent` routes every spawn without an explicit `model` | Haiku at a fifth of Opus's price where it passes; Fable only on a confident frontier call |
| no duplicate decisions | the policy tells Claude the hooks already gate commands and route spawns, so it does not also call `jev_gate_command` / `jev_route_model` | one tool round-trip and one Jev call per command and per spawn |
| quiet by default | the per-prompt note is injected only when repository context is needed | nothing added to context on prompts that need no files |
| small fixed cost | policy under 2,000 characters (loads into every session and subagent); tool descriptions cut from ~11k to ~6.7k characters | paid once per session and per subagent |

### Hooks

| event | hook | what runs |
|---|---|---|
| `SessionStart` | `session` | one line, `jev hooks active: ...`, telling the policy that gating and routing are enforced here; local, no call |
| `UserPromptSubmit` | `prompt` | one Jev call per prompt; a one-line note only when repository context is needed |
| `PreToolUse` on `Bash` | `gate-bash` | local commands: no Jev call, no prompt. Outbound ones (push, curl, deploy, publish): `jev_gate_command`. Denying the irreversible is deterministic and needs no key |
| `PreToolUse` on `Agent\|Task` | `route-agent` | appends the return contract (local, no key); sets `model` via `jev_route_model` when none was chosen |
| `PostToolUse` / `PostToolUseFailure` on `Agent\|Task` | `agent-outcome` | records whether the subagent returned or failed, keyed by `tool_use_id`; local, no call |

`python3 hooks.py report` joins each routing decision to its outcome: fallback reasons
(`low_confidence`, `invalid_response`, `transport_error`, ...), ok/error counts per
selected model, and median Jev latency. Traces store shapes and hashes, never subagent
output.

Every Jev answer is checked before it is used: the choice must be one that was offered
and the most probable one, and probabilities must lie in 0..1 and sum to 1. A malformed
answer is an `InvalidResponse` (a `TransportError`), so every caller treats it as no
decision. Questions over prompts, commands and subagent tasks tell Jev that text is
untrusted data, not instructions.

`./install.sh hooks` merges them into `~/.claude/settings.json` (user scope, so every
session) and places the defaults policy (`CLAUDE.md`) in `~/.claude/CLAUDE.md`. The two
are installed together because the policy tells Claude the hooks exist.

Because a hook runs on every event, it is built around three rules:

- **A hook never bypasses your own permission rules** on commands. On `allow` the
  command gate says nothing. An unreachable Jev, or one that has not answered within 20
  seconds, becomes `ask` — a hook that outlives Claude Code's timeout is killed and the
  call proceeds ungated, so the deadline is answered explicitly, not waited out.
- **`deny` is reserved for the irreversible**, because the user cannot override it:
  the dangerous-construct patterns and `rm` aimed at root, home or a wildcard. A plain
  `rm -f build/tmp.o`, or a line `shlex` cannot parse, is `ask` — the MCP tool's
  advisory hard block would deny both, and a gate that denies routine commands is a
  gate that gets switched off.
- **Prompt text and command lines leave the machine** — that is what a judgement
  costs. Anything credential-shaped is classified locally, held back, and turned into
  `ask` without being sent.

### Cowork, claude.ai and other sessions without the hooks

The policy keys off the `jev hooks active` line, not off where it is installed. With the
line, Claude leaves gating and routing to the hooks; without it, Claude calls
`jev_gate_command` and `jev_route_model` itself. So one conditional rule covers every
surface: a hooked Claude Code session never pays twice, and an unhooked one never goes
ungated.

To carry that rule to surfaces that do not read `~/.claude/CLAUDE.md`, put the same
condition in your claude.ai personal preferences (Settings → Profile):

> Before any action with external effect (sending, posting, deleting, paying, deploying),
> call jev_check_action. Before sending file contents to an outside service, call
> jev_classify_data; secret means stop. Before reading files to find something, call
> jev_select_context. Unless the session context says "jev hooks active": call
> jev_gate_command before shell commands that send, publish or deploy (never run
> a policy block; model verdicts are advice; local commands need no call), and
> jev_route_model before delegating (use the model it selects).

There the tools come from the remote connector (`remote_server.py`, below).

### Cloud sessions (claude.ai/code, `claude --cloud`)

A cloud session clones this repository into a fresh VM, so it can run the full local
server and hooks, not just the remote subset. `.mcp.json` and `.claude/settings.json`
point at `scripts/cloud.sh`, which acts only when `CLAUDE_CODE_REMOTE=true`: it installs
`requirements.txt` at session start, then runs `hooks.py` and `mcp_server.py` as the
user-scope install does locally. On your own machine it exits immediately, and your
local-scope `jev` server outranks the project one, so nothing runs twice.

The cloud image ships Debian-owned Python packages that pip cannot uninstall, so a
system-wide `pip install` of `mcp[cli]` fails on PyJWT and, before this was handled, the
`jev` server never started. `cloud.sh` now falls back to a git-ignored `.venv` in the
repo (about 20 seconds, once per VM, inside the SessionStart hook's budget) and runs
every later hook and the server from it.

The VM has no `.env`, and the default **Trusted** network does not reach
`openrouter.ai`. Edit the cloud environment at claude.ai/code:

- **Pro / Max:** under **API credentials**, add a Bearer credential for
  `openrouter.ai` with your OpenRouter key, and set the environment variable
  `OPENROUTER_API_KEY=injected-by-proxy`. The proxy attaches the real key after the
  request leaves the VM, so the session never sees it; the variable only tells Jev a
  key exists.
- **Team / Enterprise** (no API credentials yet): set `OPENROUTER_API_KEY=...` as an
  environment variable, switch network access to **Custom**, add `openrouter.ai`,
  and keep the default package-manager list. Anyone who can use the environment can
  read the variable.

Without a key the session still starts: the deterministic Bash hard block and the
subagent return contract hold, and the SessionStart line reports `routing off (no key)`.

### MCP tools

A local stdio process exposing eight read-only tools. It decides; it never edits files
or runs commands. `./install.sh mcp` prints the registration.

| tool | use |
|---|---|
| `jev_select_context` | **before reading files** — 508k → 3.9k tokens on a 188-file repo |
| `jev_classify_data` | before sending file contents anywhere; local-only, no model call |
| `jev_check_action` | before any action with external effect, against plain-English policy |
| `jev_file_outline` | exported symbols without loading the file; local-only |
| `jev_evaluate` | arbitrary typed decisions |
| `jev_gate_command` | commands that send, publish or deploy; the Bash hook runs it, so call directly only where no hook runs |
| `jev_route_model` | the Agent hook runs it; call directly only where no hook runs. Fable is escalation-only |
| `jev_should_run` | before a scheduled automation executes — skip runs that would find nothing |
| `jev_route_skill` | which one skill should handle a request, or `none` (pick normally). Local reads installed skills; remote takes the list. Per-message suggestion: `python3 skill_router.py on` (default off). See `skills/skill-routing` |

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
| `jev_evaluate`, `jev_should_run`, `jev_check_action`, `jev_gate_command`, `jev_route_skill` | yes | pure logic, judge what you pass them (the skill list is passed in) |
| `jev_select_context` | **no** | its saving comes from reading *your* repository; a remote version would have to upload the codebase to answer the same question |
| `jev_file_outline` | **no** | same reason |
| `jev_classify_data` → `jev_classify_paths` | reduced | the local version scans file **content** and never transmits it. A remote content scanner requires uploading the material it exists to protect — worse than none, because it is trusted. The remote variant takes paths only, and says so in its output |

So the largest token saving is inherently local. That is a property of the
problem, not a gap.

### Deploying

```bash
vercel --prod                                    # vercel.json + api/index.py included
# Vercel → Settings → Environment Variables:
#   OPENROUTER_API_KEY = your OpenRouter key (TYPESAFE_API_KEY also works)
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

## Transport

| first key set | endpoint | model |
|---|---|---|
| `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1/systemone` | `jev-latest` (`JEV_OPENROUTER_MODEL`) |
| `TYPESAFE_API_KEY` | `https://api.typesafe.ai/v1/systemone` | `jev-latest` (`JEV_MODEL`) |
| `AI_GATEWAY_API_KEY` | `https://ai-gateway.vercel.sh/v1/evaluate` | `typesafe-ai/jev` |

OpenRouter serves TypeSafe's System One API with the same request and response shapes;
it is exactly what the TypeSafe SDK calls with `base_url="https://openrouter.ai/api"`
([guide](https://openrouter.ai/docs/guides/community/typesafe-sdk)). Bare ids map into
its `typesafe/` namespace (`jev-1.13` → `typesafe/jev-1.13`, `jev-latest` →
`~typesafe/jev-latest`). Three things are specific to that path:

- **Validation.** OpenRouter rejects a `noul` without `instructions`, so a criteria-only
  noul is sent with a neutral "answer by the criteria" line on that path only.
- **Billed cost.** Every OpenRouter response carries `usage.cost`, the USD actually
  charged. `core.decide` keeps it as `Decision.cost_usd`, the model router writes it to
  its trace, and `hooks.py report` sums it (`jev_routing_cost_usd`, with
  `jev_routing_cost_source` saying whether the figure is billed, estimated at
  `JEV_PRICE_PER_MTOK`, or mixed). TypeSafe's own API does not price the call.
- **Attribution.** Requests carry `HTTP-Referer` and `X-OpenRouter-Title`
  (`JEV_OPENROUTER_APP_URL`, `JEV_OPENROUTER_APP_TITLE`; defaults: this repo's URL,
  `jev-agent`), so Jev's spend shows on its own at openrouter.ai/activity even though the
  key is shared with other tooling. A `402` means that account is out of credits.

**Pin the model once thresholds are tuned.** `jev-latest` follows TypeSafe's newest
stable release. The thresholds in this repo (route confidence 0.75, skill probability
0.60, context canary separation) were calibrated on jev-1.13; every trace records the
`model` that answered, and `JEV_OPENROUTER_MODEL=jev-1.13` (or `JEV_MODEL` on the direct
API) holds them still.

The Vercel AI Gateway is kept only for installs that still hold just
`AI_GATEWAY_API_KEY`. No transport is ever tried after another one fails,
because a silent second route would hide the first one breaking. The dialects differ
only on the wire (`noul` vs `boolean` for yes/no, snake_case vs camelCase usage), and
tests pin both.

`typesafe-sdk` remains a dependency for its question *types* only. They are
pydantic models that reject a malformed `criteria` before any network call — a
Score handed a string, a Choice handed a list — which is the single easiest
mistake to make with this API. The SDK's client is unused.

The numbers below were measured through the Vercel AI Gateway, which serves the same
Jev model. Re-measure the ceilings (MAX_QUESTIONS, MAX_PAYLOAD_CHARS) against
`api.typesafe.ai` before relying on them there.

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

`python -m pytest tests/ -q` — 85 tests, 78 of which need no key. Two of them are
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
