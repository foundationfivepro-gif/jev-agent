"""
Security-aware routing: classify data sensitivity, then let Jev choose only
among providers the policy already permits.

The classifier runs in deterministic code and fails closed. Jev never sees raw
content — only filenames, a label and the eligible provider set — so a
misclassification cannot leak the payload to the model that was supposed to
decide whether the payload may travel.

Why this is not the guide's one-line regex. That version required a keyword
followed by ':' or '=':

    (?i)(api[_-]?key|password|private[_-]?key)\\s*[:=]

which catches `API_KEY=...` and misses every other realistic shape, including
the PEM block its own verification step tells you to test with:

    MISSED  -----BEGIN RSA PRIVATE KEY-----  # pragma: allowlist secret
    MISSED  -----BEGIN OPENSSH PRIVATE KEY-----  # pragma: allowlist secret
    MISSED  aws_secret_access_token AKIA...  # pragma: allowlist secret
    MISSED  Authorization: Bearer <token>  # pragma: allowlist secret
    MISSED  {"apiKey" : "sk-live-..."}  # pragma: allowlist secret

A detector that fails open on the case it exists to catch is worse than none,
because it is trusted. This module is still not a substitute for a real scanner
— run gitleaks or detect-secrets in CI — but it fails closed and covers the
common shapes.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Iterable, Sequence

from core import Choice, decide, write_trace

# --------------------------------------------------------------- classification

# Sensitivity labels, ascending. A provider may see a label <= its max_label.
PUBLIC, INTERNAL, CUSTOMER, SECRET = 0, 1, 2, 3
LABEL_NAMES = {PUBLIC: "public", INTERNAL: "internal", CUSTOMER: "customer", SECRET: "secret"}

SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    # Key material — matched on the armour, which is the part that never varies.
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "PEM private key"),  # pragma: allowlist secret
    (r"-----BEGIN OPENSSH PRIVATE KEY-----", "OpenSSH private key"),  # pragma: allowlist secret
    (r"-----BEGIN PGP PRIVATE KEY BLOCK-----", "PGP private key"),  # pragma: allowlist secret
    # Provider-issued credentials, matched on their documented prefixes.
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
    (r"\bASIA[0-9A-Z]{16}\b", "AWS temporary access key"),
    (r"\bghp_[A-Za-z0-9]{36}\b", "GitHub personal token"),
    (r"\bgho_[A-Za-z0-9]{36}\b", "GitHub OAuth token"),
    (r"\bsk-[A-Za-z0-9]{20,}\b", "OpenAI-style secret key"),
    (r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b", "Anthropic API key"),
    (r"\bvck_[A-Za-z0-9]{20,}\b", "Vercel gateway key"),
    (r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b", "Slack token"),
    (r"\bAIza[0-9A-Za-z_\-]{35}\b", "Google API key"),
    (r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "JWT"),
    # Header form. Horizontal whitespace only — a bare \s crosses newlines and
    # then matches the next line's first word, which is a false-positive factory.
    (r"(?i)\bauthorization[ \t]*:[ \t]*(bearer|basic|token)[ \t]+\S{8,}", "Authorization header"),
    (r"(?i)\b(aws_secret_access_key|aws_session_token|client_secret)\b[^\S\n]{0,4}\S{8,}",  # pragma: allowlist secret
     "named cloud credential"),
    (r"(?i)\b(postgres|postgresql|mysql|mongodb(\+srv)?|redis|amqp)://[^\s:@/]+:[^\s@]+@",
     "connection string with password"),
)

SECRET_PATH_HINTS = (
    ".env", "id_rsa", "id_ed25519", ".pem", ".p12", ".pfx", ".keystore",
    "credentials", "secrets", ".npmrc", ".pypirc", ".netrc", "serviceaccount",
)
CUSTOMER_PATH_HINTS = ("customer", "client", "patient", "pii", "user_data", "payroll")
INTERNAL_PATH_HINTS = ("src/", "lib/", "internal/", "app/", "services/")

# A base64-ish blob this long with this much entropy is credential-shaped even
# when it matches nothing above. Tuned to sit above normal source-code strings.
_B64 = re.compile(r"[A-Za-z0-9+/=_\-]{40,}")


def _shannon(s: str) -> float:
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# A filesystem path matches _B64 (it allows "/"), and a long path of ordinary words
# easily clears the entropy bar: /Users/me/Projects/active/some-app/fix-branch scored
# 4.36. Every segment of a path is a word; segments of random base64 are not.
_PATH_WORD = re.compile(r"[A-Z]?[a-z0-9._\-]*")


def _looks_like_path(blob: str) -> bool:
    parts = [p for p in blob.split("/") if p]
    return blob.count("/") >= 2 and bool(parts) and all(_PATH_WORD.fullmatch(p) for p in parts)


def _high_entropy_blob(text: str, *, min_entropy: float = 4.2) -> str | None:
    for m in _B64.finditer(text):
        blob = m.group(0)
        if _looks_like_path(blob):
            continue
        if _shannon(blob) >= min_entropy:
            return blob[:8] + "..."
    return None


# Real scanners need an escape hatch or they flag their own test corpus and get
# switched off. A line carrying this marker is skipped — the same convention
# detect-secrets uses — and it has to be on the line itself, so adding one is a
# visible, reviewable act rather than a config file nobody reads.
ALLOWLIST_MARKER = re.compile(r"#\s*pragma:\s*allowlist secret|//\s*pragma:\s*allowlist secret")


def _scannable(text: str) -> str:
    """Drop lines the author explicitly allowlisted."""
    if "allowlist secret" not in text:
        return text
    return "\n".join(l for l in text.splitlines() if not ALLOWLIST_MARKER.search(l))


# `name = value` credential detection, done in two stages rather than one regex.
#
# A single pattern here is what produced the worst false positive in this module:
# `if label == SECRET:` matched, because a `\s` separator crossed the newline and
# swallowed the next line's first word. Any dict key, type annotation or equality
# check involving a variable named SECRET / TOKEN / PASSWORD would fire — which
# in real code means constantly, and a scanner that cries wolf gets muted.
#
# So: match the shape loosely, then decide on the VALUE in code where the rule
# can be read and tested.
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|credential|private[_-]?key)"
    r"[\"']?[ \t]*[:=][ \t]*[\"']?(?P<value>[^\s\"'`,;)}\]]{6,})"
)

# Values that are code, not credentials.
_NOT_A_SECRET = re.compile(
    r"(?i)^(none|null|true|false|undefined|str|int|bool|optional\[|list\[|dict\[|"
    r"os\.|process\.|get|read|load|fetch|input|prompt|\$\{|\{\{|<|\.\.\.|xxx+|"
    r"your[_-]|placeholder|example|redacted|changeme|secret\"?$)"
)


def _value_looks_like_a_secret(value: str) -> bool:
    """
    A credential-shaped value, as opposed to a type, a call or a placeholder.

    The test is deliberately conservative in the direction of missing rather
    than over-firing, because this pattern is the noisy one; the armour and
    provider-prefix patterns above carry the high-confidence detections.
    """
    if _NOT_A_SECRET.match(value):
        return False
    if value.endswith(("(", ")", ":")) or value.isidentifier() and value.islower():
        return False
    has_digit = any(c.isdigit() for c in value)
    mixed_case = value != value.lower() and value != value.upper()
    return len(value) >= 16 or (len(value) >= 8 and (has_digit or mixed_case))


def classify(files: Sequence[str], text: str = "") -> tuple[int, list[str]]:
    """
    Return (label, reasons). Fails closed: anything credential-shaped is SECRET.

    `text` is scanned but never returned, logged, or forwarded. Lines marked
    `pragma: allowlist secret` are skipped.
    """
    reasons: list[str] = []
    text = _scannable(text)

    for pattern, label in SECRET_PATTERNS:
        if re.search(pattern, text):
            reasons.append(f"content: {label}")

    for m in _ASSIGNMENT.finditer(text):
        if _value_looks_like_a_secret(m.group("value")):
            reasons.append("content: credential assignment")
            break

    for f in files:
        low = f.lower()
        if any(h in low for h in SECRET_PATH_HINTS):
            reasons.append(f"path: {f} looks like credential storage")
    if reasons:
        return SECRET, reasons

    blob = _high_entropy_blob(text)
    if blob:
        return SECRET, [f"content: high-entropy blob ({blob})"]

    for f in files:
        low = f.lower()
        if any(h in low for h in CUSTOMER_PATH_HINTS):
            return CUSTOMER, [f"path: {f} suggests customer data"]
    for f in files:
        if any(h in f for h in INTERNAL_PATH_HINTS):
            return INTERNAL, [f"path: {f} is internal source"]
    return PUBLIC, ["no sensitive indicators found"]


# --------------------------------------------------------------- routing

PROVIDERS = {
    "local":         {"max_label": SECRET,   "fit": "Runs on our own hardware; nothing leaves the network"},
    "approved_cloud": {"max_label": CUSTOMER, "fit": "Contracted provider with a data-processing agreement"},
    "public_cloud":  {"max_label": PUBLIC,   "fit": "General provider, suitable only for public material"},
}


def security_route(goal: str, files: Sequence[str], raw_text: str = "") -> dict:
    """
    Choose a permitted provider, or stop.

    SECRET-labelled work never reaches a provider and never reaches Jev; the
    decision is made entirely in code. Everything else is filtered to the
    eligible set first, so Jev is only ever choosing between legal options.
    """
    label, reasons = classify(files, raw_text)

    if label == SECRET:
        decision = {
            "selected": "block",
            "label": label,
            "label_name": LABEL_NAMES[label],
            "reasons": reasons,
            "source": "policy",
        }
        write_trace("security", {"goal": goal, "files": list(files)}, decision)
        return decision

    eligible = {k: v for k, v in PROVIDERS.items() if v["max_label"] >= label}
    if not eligible:
        decision = {"selected": "human", "label": label, "reasons": reasons, "source": "policy"}
        write_trace("security", {"goal": goal, "files": list(files)}, decision)
        return decision

    # raw_text is deliberately absent from state.
    state = {
        "goal": goal,
        "files": list(files),
        "data_label": LABEL_NAMES[label],
        "eligible_providers": {k: v["fit"] for k, v in eligible.items()},
    }
    criteria = {k: v["fit"] for k, v in eligible.items()}
    criteria["human"] = "None of these is appropriate; escalate to a person"

    result = decide(state, {
        "provider": Choice(
            instructions="Which permitted provider is the best fit for this work?",
            criteria=criteria,
        )
    })
    answer = result.answers["provider"]
    selected = str(answer.value) if answer.certainty >= 0.85 else "human"

    decision = {
        "selected": selected,
        "proposed": answer.value,
        "label": label,
        "label_name": LABEL_NAMES[label],
        "reasons": reasons,
        "confidence": answer.certainty,
        "source": "model",
        "probabilities": answer.probabilities,
    }
    write_trace("security", state, decision)
    return decision


if __name__ == "__main__":
    print(json.dumps(security_route("Explain auth", ["src/auth.py"], "no secrets here"), indent=2, default=str))
