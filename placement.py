"""
Where should a task run, and on which model? Local rules only: no key, no call, ~0 ms.

Policy (owner, 2026-10-22; supersedes 2026-10-08): the Mac is an option, rarely a requirement.

    cloud  default. Claude cloud session / routine, Cursor cloud agent. Repo, PR, docs,
           connector and research work.
    vps    needs a persistent machine but not this Mac: 1Password (service account),
           browser automation with a saved profile, a long-running or scheduled job,
           a localhost service. The VPS or Grok Bot's own computer, which both run the
           claude/codex CLIs. Never the Mac by default.
    mac    only what physically lives on the owner's Mac: Keychain, desktop apps or
           computer use on its screen, hardware, files in its home folders, its own
           LaunchAgents/setup, or the owner's personal signed-in Chrome.

    place("Fix the estimate form validation and open a PR")      -> where "cloud"
    place("Use op read to fetch the Stripe key and rotate it")    -> where "vps"
    place("Read the token from Keychain")                         -> where "mac"

    python3 placement.py "task text"          # prints the JSON decision

`needs_mac` lists Mac-only signals; `needs_runner` lists persistent-machine signals.
The model column maps the task's complexity (Jev's estimate when the caller has one,
else a keyword guess) onto the Sonnet-first policy: mechanical -> haiku, standard and
multi-step -> sonnet, frontier -> sonnet. Opus and Fable are never chosen here
(Opus is explicit-only, with JEV_ALLOW_OPUS_SOL=ADAMS_OK).
"""

from __future__ import annotations

import json
import re
import sys

def _signals(pairs):
    return [(name, re.compile(rx, re.I)) for name, rx in pairs]


# Needs only the owner's Mac can meet.
MAC_SIGNALS: list[tuple[str, re.Pattern]] = _signals([
    ("keychain", r"\bkey ?chain\b|security find-generic-password"),
    ("my-browser", r"\b(my|adam'?s) (chrome|browser|safari)\b|\bclaude in chrome\b|\bsafari\b"),
    ("computer-use", r"\bcomputer[- ]use\b|\bscreenshot of (my|the) (screen|desktop)\b|\bclick (on|in) the app\b"),
    ("desktop-app", r"\b(finder|system settings|notes app|messages app|imessage|photos app|xcode|simulator|desktop app)\b"),
    ("launchd", r"\blaunch ?agents?\b|\blaunchctl\b|\blaunchd\b|\bplist\b"),
    ("local-files", r"(^|\s)(~/|/Users/)(?!\S*Projects/)\S+|\b(downloads|desktop|documents) folder\b|\bicloud drive\b"),
    ("hardware", r"\b(usb|bluetooth|printer|camera|microphone|webcam|airdrop)\b"),
    ("mac-setup", r"\b(brew install|homebrew|this mac|my mac|on the mac|my laptop)\b"),
])

# Needs a persistent machine (VPS or Grok Bot's computer), not the Mac.
RUNNER_SIGNALS: list[tuple[str, re.Pattern]] = _signals([
    ("1password", r"\b1 ?password\b|\bop (run|read|inject|item)\b"),
    ("browser-session", r"\b(log ?in|logged[- ]in|sign(ed)?[- ]in)\b.{0,40}\b(browser|chrome|console|dashboard|portal|site)\b"
                        r"|\b(chrome|browser tab|playwright|headless)\b"),
    ("localhost", r"\blocalhost\b|127\.0\.0\.1|\blocal (server|service|db|database)\b"),
    ("long-running", r"\b(systemd|cron(tab)?|timer|daemon|long[- ]running|overnight|every \d+ (min|minutes|hours))\b"),
])

COMPLEXITY = ("mechanical", "standard", "multi-step", "frontier")
MODEL_FOR = {"mechanical": "haiku", "standard": "sonnet", "multi-step": "sonnet", "frontier": "sonnet"}

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
    runner = [name for name, rx in RUNNER_SIGNALS if rx.search(text)]
    if isinstance(complexity, (int, float)):
        label = COMPLEXITY[max(0, min(3, round(float(complexity))))]
        source = "jev"
    elif complexity in COMPLEXITY:
        label, source = str(complexity), "jev"
    else:
        label, source = guess_complexity(text), "keywords"
    if needs:
        where, reason = "mac", f"needs the owner's Mac ({', '.join(needs)})"
    elif runner:
        where, reason = "vps", (f"needs a persistent machine ({', '.join(runner)}): run on the VPS or Grok Bot's "
                                "computer, not the Mac")
    else:
        where, reason = "cloud", "no machine-bound need: repo, connector and web work all run in a cloud session"
    return {"where": where, "needs_mac": needs, "needs_runner": runner, "model": MODEL_FOR[label], "complexity": label,
            "complexity_source": source, "reason": reason}


def note(decision: dict, *, remote: bool) -> str | None:
    """One line for the prompt hook, or None when it would change nothing."""
    model = f"{decision['complexity']} task → {decision['model']}"
    where = decision["where"]
    if where == "mac":
        return (f"jev: {model}; only this part needs the owner's Mac ({', '.join(decision['needs_mac'])}); "
                "run everything else in a cloud session or on the VPS.")
    if where == "vps":
        return (f"jev: {model}; needs a persistent machine ({', '.join(decision['needs_runner'])}): "
                "use the VPS or Grok Bot's computer, not the Mac.")
    if remote:
        return None
    return f"jev: {model}; no machine-bound need, so this could run as a cloud session (owner prefers cloud)."


if __name__ == "__main__":
    print(json.dumps(place(" ".join(sys.argv[1:])), indent=2))
