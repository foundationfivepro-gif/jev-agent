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
