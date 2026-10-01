"""Local outbound/privacy boundary. Detection is defense in depth, never consent.

No scanner can prove arbitrary prose is public. Callers still enforce authorized
content categories and destinations. These guards have no network or file I/O.
Inline source allowlist comments cannot authorize disclosure.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

MAX_SCREEN_CHARS = 1_000_000
MAX_DEPTH = 32
REDACTED = "[redacted]"
_PERSONAL = (
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"(?i)\b(?:ssn|social.security|date.of.birth|dob|patient|diagnosis|medical.record|credit.card|bank.account)\s*[:=]\s*\S+"),
    re.compile(r"(?<!\w)(?:\+\d{1,3}[ .-])?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?!\w)"),
)
_SECRET_FIELD = re.compile(r"(?i)^(?:authorization|password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key|credential)$")


class PrivacyError(ValueError):
    """Fail-closed diagnostic intentionally containing no input or match bytes."""
    code = "privacy_unavailable"

    def __init__(self, reason: str = "sensitive content") -> None:
        # Only fixed reason labels, never externally supplied exception content.
        allowed = {"sensitive content", "invalid payload", "oversized payload", "scanner unavailable"}
        super().__init__("privacy_unavailable: " + (reason if reason in allowed else "sensitive content"))


def contains_personal_data(text: str) -> bool:
    """Conservative common personal-data indicators, not comprehensive discovery."""
    return any(pattern.search(text) for pattern in _PERSONAL)


def _sensitive(text: str) -> bool:
    from security_router import SECRET, classify
    # Scan a normalized view as well; invisible formatting cannot split a known
    # credential or personal-data pattern to bypass the boundary.
    normalized = unicodedata.normalize("NFKC", "".join(c for c in text if unicodedata.category(c) != "Cf"))
    return classify([], normalized, allow_inline_allowlist=False)[0] == SECRET or contains_personal_data(normalized)


def screen_outbound(value: Any) -> Any:
    """Reject unsafe JSON-shaped content before transport/retry construction.

    Returns the original value for convenient composition. Nothing is modified or
    persisted. Keys are screened too; sensitive field values cannot evade checks
    merely by being shorter than a detector's usual credential length.
    """
    remaining = MAX_SCREEN_CHARS
    nodes_remaining = 100000
    def visit(item: Any, depth: int = 0) -> None:
        nonlocal remaining, nodes_remaining
        nodes_remaining -= 1
        if nodes_remaining < 0:
            raise PrivacyError("oversized payload")
        if depth > MAX_DEPTH:
            raise PrivacyError("invalid payload")
        if isinstance(item, str):
            remaining -= len(item)
            if remaining < 0:
                raise PrivacyError("oversized payload")
            if _sensitive(item):
                raise PrivacyError()
        elif isinstance(item, Mapping):
            for key, val in item.items():
                if not isinstance(key, str):
                    raise PrivacyError("invalid payload")
                visit(key, depth + 1)
                if _SECRET_FIELD.fullmatch(key) and val not in (None, "", False):
                    raise PrivacyError()
                visit(val, depth + 1)
        elif isinstance(item, (list, tuple)):
            for val in item:
                visit(val, depth + 1)
        elif item is None or isinstance(item, (bool, int)):
            return
        elif isinstance(item, float) and math.isfinite(item):
            return
        else:
            raise PrivacyError("invalid payload")
    try:
        visit(value)
    except PrivacyError:
        raise
    except Exception:
        raise PrivacyError("scanner unavailable") from None
    return value


def safe_metadata(text: Any, *, max_chars: int = 512) -> str:
    """Sanitize a path, symbol, reason or opaque ID without reproducing matches."""
    if not isinstance(text, str) or len(text) > max_chars or any(unicodedata.category(c).startswith("C") for c in text):
        return REDACTED
    try:
        screen_outbound(text)
    except PrivacyError:
        return REDACTED
    return text


def sanitize_for_storage(value: Any) -> Any:
    """Copy bounded telemetry, redacting sensitive strings/keys and unknown types.

    Do not use this as authorization to store raw prompts. Prefer event IDs,
    counters and typed statuses. Rejected input/exception values are never echoed.
    """
    def clean(item: Any, depth: int = 0) -> Any:
        if depth > MAX_DEPTH:
            return REDACTED
        if isinstance(item, str):
            if len(item) > MAX_SCREEN_CHARS:
                return REDACTED
            try:
                screen_outbound(item)
            except PrivacyError:
                return REDACTED
            return item
        if isinstance(item, Mapping):
            out = {}
            for i, (key, val) in enumerate(item.items()):
                if i >= 10000:
                    out["truncated"] = True
                    break
                k = safe_metadata(key)
                if k == REDACTED:
                    # Do not store a dangerous key, nor an associated value.
                    out[f"redacted_field_{len(out)}"] = REDACTED
                else:
                    out[k] = REDACTED if _SECRET_FIELD.fullmatch(k) else clean(val, depth + 1)
            return out
        if isinstance(item, (list, tuple)):
            return [clean(v, depth + 1) for v in item[:10000]]
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float) and math.isfinite(item):
            return item
        return REDACTED
    return clean(value)
