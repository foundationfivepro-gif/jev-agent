"""Versioned shared decision-source wire contract (local and remote MCP)."""
from typing import Literal

SCHEMA_VERSION = "1.1"
CommandSource = Literal["policy", "operator_allowlist", "model", "unavailable"]
