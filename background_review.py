"""
Background review: run read-only checks in parallel without racing the writer.

Every reviewer is read-only and pinned to one snapshot. Findings computed
against a revision that has since moved are discarded, not reported — a stale
finding wastes more attention than it saves, and repeated stale findings are how
people learn to ignore the reviewer.

The gate is deliberately conservative: `passed` is true only when every selected
reviewer ran AND succeeded. A reviewer that errored is not a pass.

Measure accepted findings, not raw volume. A reviewer that emits many findings
nobody acts on is a cost, not a benefit, and should be dropped from the registry.
"""

from __future__ import annotations

import ast
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Mapping, Sequence

import canary
from core import Noul, decide_batched, write_trace

MIN_RELEVANCE = 0.55
REVIEW_TIMEOUT = 90


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=REVIEW_TIMEOUT)


def _snapshot() -> str:
    r = _git("rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else "no-git"


def syntax_review() -> dict:
    errors = []
    for path in Path(".").rglob("*.py"):
        if ".venv" in path.parts or "site-packages" in path.parts:
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            errors.append(f"{path}:{exc.lineno}: {exc.msg}")
    return {"ok": not errors, "findings": errors[:20], "count": len(errors)}


def whitespace_review() -> dict:
    r = _git("diff", "--check")
    out = (r.stdout + r.stderr).splitlines()
    return {"ok": r.returncode == 0, "findings": out[:20], "count": len(out)}


def tests_review() -> dict:
    r = subprocess.run(["python3", "-m", "pytest", "--collect-only", "-q"],
                       capture_output=True, text=True, timeout=REVIEW_TIMEOUT)
    # 5 means "no tests collected", which is not a failure of the reviewer.
    return {"ok": r.returncode in (0, 5), "findings": r.stdout.splitlines()[-6:],
            "count": 0 if r.returncode in (0, 5) else 1}


def secrets_review() -> dict:
    """Reuses the classifier from security_router rather than a second regex."""
    from security_router import SECRET, classify

    findings = []
    for path in list(Path(".").rglob("*.py"))[:400]:
        # Test fixtures contain deliberate secrets. Excluding them here rather
        # than allowlisting each one keeps the signal meaningful; a real secret
        # committed under tests/ is caught by CI scanning, not by this gate.
        if {".venv", "site-packages", "tests"} & set(path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        label, why = classify([str(path)], text)
        if label == SECRET:
            findings.append(f"{path}: {why[0]}")
    return {"ok": not findings, "findings": findings[:20], "count": len(findings)}


REVIEWERS: Mapping[str, Callable[[], dict]] = {
    "syntax": syntax_review,
    "whitespace": whitespace_review,
    "tests": tests_review,
    "secrets": secrets_review,
}


def _question(name: str) -> Noul:
    return Noul(
        instructions=(
            f"Consider ONLY the reviewer named '{name}' in state.reviewers, "
            f"ignoring every other reviewer. Is it worth running for this change?"
        )
    )


def background_review(
    goal: str,
    changed_files: Sequence[str],
    *,
    min_relevance: float = MIN_RELEVANCE,
    always: Sequence[str] = ("secrets",),
) -> dict:
    """
    Select and run read-only reviewers in parallel.

    Reviewers in `always` bypass scoring. A secrets check that a model can
    decline to run is not a control, so it is deterministic by default.
    """
    state_reviewers = {
        name: {"checks": fn.__doc__ or name} for name, fn in REVIEWERS.items() if name not in always
    }
    questions = {name: _question(name) for name in state_reviewers}

    for probe, text in (
        (canary.CANARY_RELEVANT["id"], "A reviewer that checks exactly what this change touches."),
        (canary.CANARY_IRRELEVANT["id"], "A reviewer that validates cafeteria menus from 1998."),
    ):
        state_reviewers[probe] = {"checks": text}
        questions[probe] = _question(probe)

    def state_for(ids):
        keep = set(ids) | set(canary.CANARY_IDS)
        return {"goal": goal, "changed_files": list(changed_files),
                "reviewers": {k: v for k, v in state_reviewers.items() if k in keep}}

    result = decide_batched(state_for, questions)
    probe_result = canary.check_separation(
        result, [canary.CANARY_RELEVANT["id"]], [canary.CANARY_IRRELEVANT["id"]]
    ).raise_if_failed()

    selected = list(always) + [
        n for n in REVIEWERS
        if n not in always and float(result.value(n, 0.0)) >= min_relevance
    ]

    before = _snapshot()
    outputs: dict[str, dict] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(selected))) as pool:
        jobs = {n: pool.submit(REVIEWERS[n]) for n in selected}
        for n, job in jobs.items():
            try:
                outputs[n] = job.result(timeout=REVIEW_TIMEOUT + 5)
            except Exception as exc:
                errors[n] = f"{type(exc).__name__}: {exc}"
    after = _snapshot()

    stale = before != after
    passed = (not stale) and (not errors) and all(o["ok"] for o in outputs.values())

    payload = {
        "selected": selected,
        "snapshot": before,
        "stale": stale,
        "outputs": {} if stale else outputs,
        "errors": errors,
        "passed": passed,
        "canary": probe_result.detail,
        "note": "discarded: repository moved while reviewers ran" if stale else "",
    }
    write_trace("background_review", {"goal": goal, "changed_files": list(changed_files)},
                {"selected": selected, "passed": passed}, payload)
    return payload


if __name__ == "__main__":
    out = background_review("Verify the patch", ["core.py"])
    print(json.dumps({k: out[k] for k in ("selected", "passed", "errors", "canary")}, indent=2))
