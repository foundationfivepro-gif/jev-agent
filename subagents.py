"""
Dynamic subagents: spawn read-only workers only when parallelism actually pays.

The rule that survives price changes is not "is this task hard" but **how much
the worker compresses**. A worker that reads fifty files and returns three lines
wins at any price. One that hands back everything it saw loses at any price,
because the parent pays full input cost to absorb the transcript.

So every worker here returns a bounded, machine-readable result — never a
transcript — and every worker is read-only. Parallel writers need leases, base
revisions and conflict detection; none of that is here, and adding writers
without it is how two agents silently overwrite each other.

Results are pinned to a snapshot. A result computed against a revision that has
since moved is discarded rather than merged.
"""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Mapping

from core import Choice, decide, write_trace

MIN_CONFIDENCE = 0.75
WORKER_TIMEOUT = 60


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=WORKER_TIMEOUT)


def _snapshot() -> str:
    """The revision every worker's result is tied to."""
    r = _git("rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else "no-git"


# Each worker returns a small dict. The bound is the point: an unbounded return
# destroys the compression ratio that makes delegation worth doing at all.

def files_worker() -> dict:
    paths = [str(p) for p in Path(".").rglob("*.py") if ".venv" not in p.parts][:200]
    return {"count": len(paths), "sample": paths[:25]}


def diff_worker() -> dict:
    r = _git("diff", "--name-only")
    changed = r.stdout.splitlines()
    return {"changed_count": len(changed), "changed": changed[:40], "returncode": r.returncode}


def tests_worker() -> dict:
    r = subprocess.run(["python3", "-m", "pytest", "--collect-only", "-q"],
                       capture_output=True, text=True, timeout=WORKER_TIMEOUT)
    tail = r.stdout.splitlines()[-8:]
    return {"returncode": r.returncode, "tail": tail}


WORKERS: Mapping[str, Callable[[], dict]] = {
    "files": files_worker,
    "diff": diff_worker,
    "tests": tests_worker,
}

PLANS: Mapping[str, list[str]] = {
    "solo": [],
    "inspect": ["files", "diff"],
    "verify": ["files", "diff", "tests"],
}


def run_subagents(goal: str, *, min_confidence: float = MIN_CONFIDENCE) -> dict:
    """Choose the smallest sufficient worker plan and run it."""
    state = {"goal": goal, "available_workers": sorted(WORKERS)}
    result = decide(state, {
        "plan": Choice(
            instructions="What is the smallest sufficient plan of read-only parallel workers?",
            criteria={
                "solo": "No parallel discovery is needed; the main agent has enough",
                "inspect": "List files and inspect what changed",
                "verify": "Inspect files and changes, and collect the test suite",
            },
        )
    })
    answer = result.answers["plan"]
    # Fail closed toward doing less: an uncertain plan means no parallel work
    # rather than speculative fan-out whose results may not be used.
    plan = str(answer.value) if answer.certainty >= min_confidence else "solo"
    if plan not in PLANS:
        plan = "solo"

    before = _snapshot()
    outputs: dict[str, dict] = {}
    errors: dict[str, str] = {}
    if PLANS[plan]:
        with ThreadPoolExecutor(max_workers=len(PLANS[plan])) as pool:
            jobs = {name: pool.submit(WORKERS[name]) for name in PLANS[plan]}
            for name, job in jobs.items():
                try:
                    outputs[name] = job.result(timeout=WORKER_TIMEOUT + 5)
                except Exception as exc:
                    errors[name] = f"{type(exc).__name__}: {exc}"
    after = _snapshot()

    stale = before != after
    payload = {
        "plan": plan,
        "proposed": answer.value,
        "confidence": answer.certainty,
        "snapshot": before,
        "stale": stale,
        "outputs": {} if stale else outputs,
        "errors": errors,
        "note": "discarded: repository moved while workers ran" if stale else "",
    }
    write_trace("subagents", state, {"plan": plan, "stale": stale}, payload)
    return payload


if __name__ == "__main__":
    print(json.dumps(run_subagents("Prepare an auth refactor"), indent=2, default=str))
