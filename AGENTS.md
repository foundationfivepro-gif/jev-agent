<!-- foundation-five:graft:start -->
## Local code context (Graft)

Graft CLI and the Codex Graft MCP server provide a local structural code graph.
Use the graph to locate code and trace dependencies; verify relevant source and
tests before changing behavior. Source and tests remain authoritative. Use `rg`
for unsupported files, exhaustive text searches, and when graph results are
missing or stale. A refresh warning means the graph may be stale.

The machine-wide launcher initializes this checkout before MCP startup. If the
tools are unavailable, run `graft-bootstrap --root "$PWD"`, then use `graft ask`,
`graft callers`, `graft skeleton`, or `graft map` with this checkout as the explicit
directory argument. Keep each worktree's `graft/` cache separate and git-ignored.
Do not run `graft init` to replace the managed machine integration. Do not enable
`--deep` or other model-backed operations without provider and spending approval.
New-project setup and first deployment include `graft-project-setup --root "$PWD"`: one bounded touched-file Codex AI pass, deferred until source exists. This managed initial pass is standing-authorized. Routine MCP refresh remains structural. After source edits and before deployment run `graft-refresh --root "$PWD" --file relative/source/path` for files changed in this task (repeat --file as needed). This incremental AI pass accepts the caller's configured call and token budgets and does not rebuild whole-project concepts. Invalid multi-file AI batches fall back once to isolated per-file requests; a failed isolated request is retained without retry.
Notion remains the source for scoped project decisions, tasks and work history.
The shared `graft-enrich` command uses the optimized batched Codex adapter (up to eight file summaries or four symbol-file fragments per request). Use this managed command rather than stock `graft --deep`; its current implementation is loaded fresh on every invocation. Existing sessions can invoke it immediately.
Setup reports structural availability separately from optional AI enrichment. A successful structural build stays successful when AI is partial, deferred, or unavailable. Actual structural errors still fail and must be reported.
Every bootstrap/MCP startup writes `graft/.cache/structural-receipt.json` with checkout/ref, a timestamped source-hash freshness check and a real Graft repository-map query. Setup also writes `graft/.cache/setup-receipt.json` with separate structural and enrichment status. Use `graft-project-setup --root "$PWD" --structural-only` to verify availability and refresh receipts without model calls.
Receipts are point-in-time observations, not decision authority. In a new project decision receipt cite the operational receipt and its checkedAt/ref, then record the task-specific dependency query and separately verified source/tests. Never rewrite historical review evidence or claim a map query proves task-specific dependency analysis. An optional AI warning alone does not mean structural Graft is unavailable.
<!-- foundation-five:graft:end -->
