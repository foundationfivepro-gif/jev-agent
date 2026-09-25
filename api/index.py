"""
Vercel entry point.

The ASGI app lives in remote_server.py at the project root; this module only
makes the root importable, because Vercel executes functions from api/.

Environment variables to set in the Vercel project (Settings -> Environment
Variables), not in this file:

    AI_GATEWAY_API_KEY   the Jev gateway key the tools spend
    JEV_REMOTE_TOKEN     the shared secret clients must present; without it the
                         server refuses every request rather than serving an
                         open endpoint that spends your quota
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from remote_server import app  # noqa: E402,F401  — Vercel serves this symbol

# TEMPORARY debug shim: echoes routing fields (never header values) when the
# request carries x-jev-debug. Removed once path restoration is fixed.
_inner = app


async def app(scope, receive, send):  # noqa: F811
    if scope.get("type") == "http" and any(k == b"x-jev-debug" for k, _ in scope.get("headers", [])):
        import json
        hdrs = {k.decode(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        body = json.dumps({
            "path": scope.get("path"),
            "raw_path": (scope.get("raw_path") or b"").decode("latin-1"),
            "root_path": scope.get("root_path"),
            "query_string": scope.get("query_string", b"").decode("latin-1"),
            "header_names": sorted(hdrs),
            "path_like_headers": {k: v for k, v in hdrs.items()
                                  if k not in ("authorization", "cookie", "x-api-key")
                                  and ("path" in k or "url" in k or "uri" in k or "route" in k or "match" in k)},
        }).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})
        return
    await _inner(scope, receive, send)
