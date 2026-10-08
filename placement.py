"""
Where should a task run, and on which model? Local rules only: no key, no call, ~0 ms.

Policy (owner, 2026-10-08): minimise local sessions. A task runs in a cloud session
(claude.ai/code, `claude --cloud`, a routine) unless it needs this Mac:

    local files outside a GitHub repo, Keychain or 1Password, a logged-in browser or
    desktop app, computer use, LaunchAgents/launchd, localhost services, or hardware.

A cloud session clones the repo, runs the repo's .claude hooks and has the claude.ai
connectors, so ordinary code, PR, docs, data-via-connector and research work fits there.

    place("Fix the estimate form validation and open a PR")
      -> {"where": "cloud", "needs_mac": [], "model": "sonnet", ...}
    place("Read the token from Keychain and update the LaunchAgent")
      -> {"where": "local", "needs_mac": ["keychain", "launchd"], ...}

    python3 placement.py "task text"          # prints the JSON decision

The model column maps the task's complexity (Jev's estimate when the caller has one,
else a keyword guess) onto the Sonnet-first policy: mechanical -> haiku, standard and
multi-step -> sonnet, frontier -> opus. Fable is never chosen here.
"""

from __future__ import annotations

import json
import re
import sys

# Each signal: a name and a pattern for a need only this Mac can meet.
MAC_SIGNALS: list[tuple[str, re.Pattern]] = [(name, re.compile(rx, re.I)) for name, rx in [
    ("keychain", r"\bkey ?chain\b|security find-generic-password"),
    ("1password", r"\b1 ?password\b|\bop (run|read|inject|item)\b"),
    ("browser-session", r"\b(log ?in|logged[- ]in|sign(ed)?[- ]in)\b.{0,40}\b(browser|chrome|safari|console|dashboard|portal|site)\b"
                        r"|\b(chrome|safari|claude in chrome|browser tab|my browser)\b"),
    ("computer-use", r"\bcomputer[- ]use\b|\bscreenshot of (my|the) (screen|desktop)\b|\bclick (on|in) the app\b"),
    ("desktop-app", r"\b(finder|system settings|notes app|messages app|imessage|photos app|xcode|simulator|desktop app)\b"),
    ("launchd", r"\blaunch ?agents?\b|\blaunchctl\b|\blaunchd\b|\bplist\b"),
    ("localhost", r"\blocalhost\b|127\.0\.0\.1|\blocal (server|service|db|database)\b"),
    ("local-files", r"(^|\s)(~/|/Users/)(?!\S*Projects/)\S+|\b(downloads|desktop|documents) folder\b|\bicloud drive\b"),
    ("hardware", r"\b(usb|bluetooth|printer|camera|microphone|webcam|airdrop)\b"),
    ("mac-setup", r"\b(brew install|homebrew|~/\.claude/settings|~/\.codex|this mac|my mac|on the mac)\b"),
]]

COMPLEXITY = ("mechanical", "standard", "multi-step", "frontier")
MODEL_FOR = {"mechanical": "haiku", "standard": "sonnet", "multi-step": "sonnet", "frontier": "opus"}

_MECHANICAL = re.compile(r"^\s*(rename|typo|bump|format|lint|reword|fix (a|the) typo|update the (date|version)|"
                         r"list|count|show|what (is|are)|which)\b", re.I)
_FRONTIER = re.compile(r"\b(architect(ure)?|redesign|migrat(e|ion) (the|all)|security (audit|review)|"
                       r"novel|research and design|end-to-end design|rewrite the (whole|entire))\b", re.I)
_MULTI = re.compile(r"\b(and then|across (all|every|the) (repos|files)|multi-step|refactor|"
                    r"several|each of|sweep|pipeline)\b", re.I)


def guess_complexity(task: str) -> str:
    """A keyword estimate, used only when the caller has no Jev complexity score."""
    if _FRONTIER.search(task):
        return "frontier"
    if _MULTI.search(task) or len(task) > 600:
        return "multi-step"
    if _MECHANICAL.search(task) and len(task) < 160:
        return "mechanical"
    return "standard"


def place(task: str, *, complexity: str | float | None = None) -> dict:
    """Local or cloud, the model, and why. `complexity`: a COMPLEXITY label or Jev's 0-3 score."""
    text = task or ""
    needs = [name for name, rx in MAC_SIGNALS if rx.search(text)]
    if isinstance(complexity, (int, float)):
        label = COMPLEXITY[max(0, min(3, round(float(complexity))))]
        source = "jev"
    elif complexity in COMPLEXITY:
        label, source = str(complexity), "jev"
    else:
        label, source = guess_complexity(text), "keywords"
    where = "local" if needs else "cloud"
    reason = (f"needs this Mac ({', '.join(needs)})" if needs
              else "no Mac-only need: repo, connector and web work all run in a cloud session")
    return {"where": where, "needs_mac": needs, "model": MODEL_FOR[label], "complexity": label,
            "complexity_source": source, "reason": reason}


def note(decision: dict, *, remote: bool) -> str | None:
    """One line for the prompt hook, or None when it would change nothing."""
    model = f"{decision['complexity']} task → {decision['model']}"
    if remote:
        if decision["where"] == "local":
            return (f"jev: {model}; this task needs the Mac ({', '.join(decision['needs_mac'])}), "
                    "which a cloud session can't reach: hand that part to a local session.")
        return None
    if decision["where"] == "cloud":
        return f"jev: {model}; no Mac-only need, so this could run as a cloud session (owner prefers cloud)."
    return f"jev: {model}; local is right here ({', '.join(decision['needs_mac'])})."


if __name__ == "__main__":
    print(json.dumps(place(" ".join(sys.argv[1:])), indent=2))
