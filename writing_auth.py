"""Offline OAuth 2.1 boundary prototype. This is NOT a production issuer.

There is deliberately no HTTP client, discovery fetch, JWT trust-on-first-use,
environment-variable access, or upstream API-key support. The only accepted
tokens are explicitly issued *synthetic* test records. A real implementation
requires a separately approved issuer, signing-key verification, durable
revocation and quotas, and independently reviewed host discovery integration.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable
from urllib.parse import unquote, urlsplit


class AuthFailure(Exception):
    """Contains only a stable public code, never the rejected token."""

    def __init__(self, code: str = "unauthenticated"):
        self.code = code
        super().__init__(code)


def safe_endpoint(value: str) -> str:
    """Reject credentials, token-in-path/query workarounds and non-HTTPS URLs."""
    try:
        parsed = urlsplit(value)
        decoded = unquote(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or any(ord(character) < 33 or ord(character) == 127 for character in decoded)
                or re.search(r"/(?:t|token|bearer)/", unquote(parsed.path), re.I)
                or re.search(r"(?:access[_-]?token|api[_-]?key|password|secret|credential)\s*[:=]", decoded, re.I)
                or re.search(r"(?:sk-|eyJ|dummy-token-)", decoded)):
            raise ValueError
        parsed.port  # Reject malformed ports without reporting the URL.
    except (ValueError, TypeError, AttributeError):
        raise ValueError("unsafe_oauth_endpoint") from None
    return value.rstrip("/")


def pkce_challenge(verifier: str) -> str:
    if not isinstance(verifier, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
        raise AuthFailure("invalid_pkce")
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    subject: str
    scopes: frozenset[str]
    # Explicit server-side consent is destination AND data-class bounded.
    grants: frozenset[tuple[str, str]]

    def allows(self, destination: str, data_class: str) -> bool:
        return (destination, data_class) in self.grants


@dataclass(frozen=True)
class MockToken:
    principal: Principal
    issuer: str
    audience: str
    expires_at: float


class MockOAuthBoundary:
    """In-memory synthetic auth, PKCE and revocation with tenant isolation.

    Registrations and grants are provided by trusted fixture setup, never by
    request bodies. Authorization codes are single-use and bind client ID,
    redirect URI, resource and S256 challenge. No browser or actual grant opens.
    """

    mode = "mock"

    def __init__(self, *, issuer: str = "https://mock-issuer.invalid",
                 audience: str = "https://mock-jev.invalid/mcp",
                 clock: Callable[[], float] = time.time):
        self.issuer = safe_endpoint(issuer)
        self.audience = safe_endpoint(audience)
        self.clock = clock
        self._lock = threading.RLock()
        self._tokens: dict[str, MockToken] = {}
        self._clients: dict[str, frozenset[str]] = {}
        self._codes: dict[str, tuple] = {}
        self._revoked: set[str] = set()

    def _now(self) -> float:
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise AuthFailure("invalid_auth_clock")
        return now

    def authorization_metadata(self) -> dict:
        return {"issuer": self.issuer,
                "authorization_endpoint": self.issuer + "/authorize",
                "token_endpoint": self.issuer + "/token",
                "revocation_endpoint": self.issuer + "/revoke",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": ["writing:plan", "writing:generate", "writing:read"]}

    def protected_resource_metadata(self) -> dict:
        return {"resource": self.audience, "authorization_servers": [self.issuer],
                "bearer_methods_supported": ["header"],
                "scopes_supported": ["writing:plan", "writing:generate", "writing:read"]}

    def register_mock_client(self, client_id: str, redirect_uris: list[str]) -> None:
        if not client_id or not redirect_uris:
            raise ValueError("invalid_mock_client")
        uris = frozenset(safe_endpoint(uri) for uri in redirect_uris)
        with self._lock:
            self._clients[client_id] = uris

    def authorize_mock(self, *, client_id: str, redirect_uri: str, resource: str,
                       code_challenge: str, code_challenge_method: str,
                       principal: Principal) -> str:
        with self._lock:
            if (redirect_uri not in self._clients.get(client_id, ())
                    or resource != self.audience or code_challenge_method != "S256"
                    or not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge)):
                raise AuthFailure("invalid_authorization_request")
            code = "dummy-code-" + uuid.uuid4().hex
            self._codes[code] = (client_id, redirect_uri, resource, code_challenge,
                                 principal, self._now() + 60)
            return code

    def exchange_mock(self, *, code: str, client_id: str, redirect_uri: str,
                      resource: str, code_verifier: str) -> str:
        with self._lock:
            record = self._codes.pop(code, None)
            if record is None:
                raise AuthFailure("invalid_grant")
            client, redirect, audience, challenge, principal, expiry = record
            if (client != client_id or redirect != redirect_uri or audience != resource
                    or not math.isfinite(expiry) or expiry <= self._now()
                    or not hmac.compare_digest(challenge, pkce_challenge(code_verifier))):
                raise AuthFailure("invalid_grant")
            return self.issue_mock_token(principal)

    def issue_mock_token(self, principal: Principal, *, ttl: float = 300,
                         issuer: str | None = None, audience: str | None = None) -> str:
        if type(ttl) not in (float, int) or not math.isfinite(ttl) or not 0 < ttl <= 86400:
            raise ValueError("invalid_mock_token_ttl")
        with self._lock:
            expires_at = self._now() + ttl
            if not math.isfinite(expires_at):
                raise ValueError("invalid_mock_token_expiry")
            token = "dummy-token-" + uuid.uuid4().hex
            self._tokens[token] = MockToken(principal, issuer or self.issuer,
                                           audience or self.audience, expires_at)
            return token

    def authenticate(self, token: str | None, scope: str) -> Principal:
        with self._lock:
            # No JWT parsing, key fallback, token URL or raw token in any error.
            record = self._tokens.get(token) if isinstance(token, str) else None
            now = self._now()
            if (record is None or token in self._revoked or not math.isfinite(record.expires_at)
                    or record.expires_at <= now
                    or record.issuer != self.issuer or record.audience != self.audience):
                raise AuthFailure()
            if scope not in record.principal.scopes:
                raise AuthFailure("unauthorized")
            return record.principal

    def revoke_mock(self, token: str) -> None:
        with self._lock:
            self._revoked.add(token)
