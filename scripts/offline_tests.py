#!/usr/bin/env python3
"""Run real pytest with a fresh credential-free environment and network guard.

Install requirements beforehand. This script neither loads .env nor invokes pip.
No faked SDKs are used. Local in-process ASGI and injected transports still work.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory(prefix="jev-offline-") as tmp:
    env = {
        "PATH": str(Path(sys.executable).parent) + os.pathsep + "/usr/bin:/bin",
        "HOME": tmp,
        "TMPDIR": tmp,
        "PYTHONPATH": str(root / "scripts" / "offline_guard") + os.pathsep + str(root),
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "JEV_ENV_FILE": os.devnull,
        "JEV_TRACE_DIR": str(Path(tmp) / "traces"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", *sys.argv[1:]],
        cwd=root, env=env,
    )
    raise SystemExit(result.returncode)
