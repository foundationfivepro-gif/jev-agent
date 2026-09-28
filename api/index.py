"""
Vercel entry point.

The ASGI app lives in remote_server.py at the project root; this module only
makes the root importable, because Vercel executes functions from api/.

Environment variables to set in the Vercel project (Settings -> Environment
Variables), not in this file:

    OPENROUTER_API_KEY   the OpenRouter key the tools spend (openrouter.ai System
                         One API); TYPESAFE_API_KEY and the legacy AI_GATEWAY_API_KEY
                         still work if no OpenRouter key is set
    JEV_REMOTE_TOKEN     the shared secret clients must present; without it the
                         server refuses every request rather than serving an
                         open endpoint that spends your quota
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from urllib.parse import parse_qsl, urlencode  # noqa: E402

from remote_server import app as _app  # noqa: E402

# vercel.json rewrites every URL to /api/index, and Vercel hands the function
# that rewritten path with no trace of the original. The rewrite carries the
# original path in __jev_path; put it back so routing and the /health auth
# exemption see the URL the client actually requested.
_PARAM = "__jev_path"


async def app(scope, receive, send):
    if scope.get("type") in ("http", "websocket"):
        pairs = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        original = next((v for k, v in pairs if k == _PARAM), None)
        if original is not None:
            path = "/" + original.lstrip("/")
            rest = urlencode([(k, v) for k, v in pairs if k != _PARAM])
            scope = dict(scope, path=path, raw_path=path.encode("latin-1"),
                         query_string=rest.encode("latin-1"))
    await _app(scope, receive, send)
