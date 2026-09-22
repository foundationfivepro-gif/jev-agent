"""
The integration boundary: Jev prepares and gates a packet; your executor does
the work.

Ordering is not arbitrary. Security runs first, because a classification that
happens after the content has been sent is not a control. Permission runs before
anything executes. Context and rules are assembled only once the work is allowed
to proceed, so a blocked request costs almost nothing.

Nothing here writes. `control_loop` returns a packet and stops; the caller hands
it to an executor and brings back a receipt. That keeps the whole package
read-only and makes every stage independently testable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from conditional_agents import select_rules
from context_tier import Chunk, select
from model_router import route_model
from permission_gate import gate
from security_router import security_route


@dataclass
class Packet:
    status: str                       # 'ready' | 'stopped'
    stage: str = ""                   # which stage stopped it
    request: str = ""
    files: list[str] = field(default_factory=list)
    context: str = ""
    instructions: list[str] = field(default_factory=list)
    model: str = ""
    command: str = ""
    decisions: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


def control_loop(
    request: str,
    files: Sequence[str],
    chunks: Sequence[Chunk],
    proposed_command: str = "",
    *,
    raw_text: str = "",
    needs_browser: bool = False,
) -> Packet:
    """
    Run every gate in order and return an execution packet.

    Stops at the first gate that refuses. `raw_text` is scanned for secrets and
    never forwarded to any model.
    """
    decisions: dict[str, Any] = {}

    security = security_route(request, files, raw_text)
    decisions["security"] = security
    if security["selected"] in ("block", "human"):
        return Packet(status="stopped", stage="security", request=request,
                      files=list(files), decisions=decisions)

    if proposed_command:
        permission = gate(proposed_command)
        decisions["permission"] = permission
        if permission["final"] != "allow":
            return Packet(status="stopped", stage="permission", request=request,
                          files=list(files), command=proposed_command, decisions=decisions)

    rules = select_rules(request, files)
    decisions["rules"] = {"rule_ids": rules["rule_ids"], "always": rules["always"]}

    packed = select(request, chunks)
    decisions["context"] = {
        "included": [c.id for c in packed.included],
        "indexed": len(packed.indexed),
        "excluded": len(packed.excluded),
        "summary": packed.summary(),
    }

    model = route_model(request, needs_browser=needs_browser)
    decisions["model"] = model

    return Packet(
        status="ready",
        request=request,
        files=list(files),
        context=packed.render(),
        instructions=rules["instructions"],
        model=model["selected"],
        command=proposed_command,
        decisions=decisions,
    )


# ---------------------------------------------------------------- executor

def execute(packet: Packet) -> dict:
    """
    Replace this with one adapter to your coding agent.

    The contract, which matters more than the implementation:
      * accept one immutable packet
      * perform ONE observable stage, not an open-ended loop
      * return artifact paths, tool receipts and errors
      * read fresh state before the next decision
      * never treat an agent's own "done" as proof that anything happened
    """
    raise NotImplementedError("connect your executor here")


if __name__ == "__main__":
    demo_chunks = [
        Chunk("goal", "Fix the login redirect loop", kind="request"),
        Chunk("auth", "def login(u):\n    return redirect('/home')\n" * 40, path="src/auth/session.py"),
        Chunk("css", "body { color: red }\n" * 60, path="web/styles/theme.css"),
    ]
    for label, cmd, text in (
        ("safe request", "pytest -q", "no secrets"),
        ("unsafe command", "rm -rf /", "no secrets"),
        ("secret present", "pytest -q", "-----BEGIN RSA PRIVATE KEY-----"),  # pragma: allowlist secret
    ):
        p = control_loop("Fix the login redirect loop", ["src/auth/login.py"],
                         demo_chunks, cmd, raw_text=text)
        print(f"{label:16} -> {p.status:8} {p.stage or 'model=' + p.model}")
