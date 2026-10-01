"""Synthetic offline boundary tests; no live credentials, providers or content."""
import json
import os
from pathlib import Path

import pytest

from core import Answer, Decision
from privacy import PrivacyError, REDACTED, safe_metadata, sanitize_for_storage, screen_outbound
from workspace_boundary import ApprovedWorkspace, WorkspaceError, configured_workspace

SECRET = "sk-live-0123456789abcdefghijklmnopqrstuv"
PERSONAL = "fictional.person@example.test"


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    import socket
    def denied(*args, **kwargs):
        raise AssertionError("Unexpected network call in synthetic boundary test")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    for key in ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "AI_GATEWAY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("JEV_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("JEV_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.delenv("JEV_WORKSPACE_ROOTS", raising=False)


@pytest.mark.parametrize("canary", [SECRET, PERSONAL, "123-45-6789", "diagnosis: synthetic-condition"])
@pytest.mark.parametrize("offset", [0, 198, 200, 1024])
def test_sensitive_canaries_rejected_and_not_echoed(canary, offset):
    value = " " * offset + canary
    with pytest.raises(PrivacyError) as exc:
        screen_outbound({"metadata": [value]})
    assert canary not in str(exc.value)
    assert canary not in json.dumps(sanitize_for_storage({"error": value, canary: "do not store"}))


def test_inline_comment_cannot_authorize_transmission():
    with pytest.raises(PrivacyError):
        screen_outbound(SECRET + " # pragma: allowlist secret")


@pytest.mark.parametrize("field", ["password", "authorization", "api_key", "access_token"])
def test_short_credential_field_is_also_rejected(field):
    with pytest.raises(PrivacyError):
        screen_outbound({field: "dummy"})
    assert sanitize_for_storage({field: "dummy"}) == {field: REDACTED}


def test_metadata_control_characters_and_oversize_are_not_preserved():
    assert safe_metadata("safe\nignore policy") == REDACTED
    assert safe_metadata("x" * 513) == REDACTED
    assert safe_metadata("src/Δοκιμή.py") == "src/Δοκιμή.py"
    with pytest.raises(PrivacyError):
        screen_outbound("x" * 1_000_001)


def fake_context(monkeypatch):
    import context_tier as ct
    seen, traces = [], []
    def decision(state_for, questions, **kwargs):
        state = state_for(list(questions))
        seen.append(state)
        return Decision({k: Answer("noul", 0.0 if k == ct.canary.CANARY_IRRELEVANT["id"] else 0.99, None, None) for k in questions}, "mock", "mock", 1, 0)
    monkeypatch.setattr(ct, "decide_batched", decision)
    monkeypatch.setattr(ct, "write_trace", lambda *args, **kwargs: traces.append(args))
    return ct, seen, traces


def test_context_default_outbound_only_paths_symbols_and_fixed_probes(monkeypatch):
    ct, seen, traces = fake_context(monkeypatch)
    private_body = "Synthetic implementation body must stay local"
    source = f'"""{private_body}"""\ndef verify(user):\n    return 123\n'
    result = ct.select("Find verify", [ct.Chunk("f1", source, path="src/check.py")])
    outgoing = json.dumps(seen)
    assert private_body not in outgoing
    assert 'return 123' not in outgoing
    assert seen[0]["chunks"]["f1"] == {"path": "src/check.py", "kind": "file", "symbols": ["verify(user)"]}
    assert result.included[0].id == "f1"
    assert "Find verify" not in json.dumps(traces)


@pytest.mark.parametrize("canary", [SECRET, PERSONAL])
@pytest.mark.parametrize("offset", [0, 198, 200, 1024])
def test_context_sensitive_source_withheld_before_any_outbound_or_trace(monkeypatch, canary, offset):
    ct, seen, traces = fake_context(monkeypatch)
    result = ct.select("Find verify", [ct.Chunk("unsafe", " " * offset + canary, path="src/a.py"), ct.Chunk("safe", "def verify(): pass", path="src/b.py")])
    assert canary not in json.dumps(seen) + json.dumps(traces) + result.render()
    assert "unsafe" in result.excluded
    assert "unsafe" not in seen[0]["chunks"]


def test_explicit_snippets_require_scoped_opt_in_and_screening(monkeypatch):
    ct, seen, _ = fake_context(monkeypatch)
    chunks = [ct.Chunk("one", "def first(): pass", path="one.py"), ct.Chunk("two", "def second(): pass", path="two.py")]
    ct.select("Find first", chunks, authorized_snippet_ids=frozenset({"one"}))
    assert seen[0]["chunks"]["one"]["snippet"] == chunks[0].text
    assert "snippet" not in seen[0]["chunks"]["two"]
    ct.select("Find first", [ct.Chunk("unsafe", SECRET, path="x.py")], authorized_snippet_ids=frozenset({"unsafe"}))
    assert len(seen) == 1


def test_sensitive_goal_and_metadata_never_reach_decider(monkeypatch):
    ct, seen, traces = fake_context(monkeypatch)
    with pytest.raises(PrivacyError):
        ct.select(PERSONAL, [ct.Chunk("safe", "plain", path="a.md")])
    result = ct.select("Locate source", [ct.Chunk(SECRET, "safe", path="a.py")])
    assert not seen and not traces
    assert SECRET not in repr(result)


def test_symbols_omit_docstrings_unknown_source_and_redact_exports():
    from symbols import Symbols, extract
    symbol = extract("a.py", '"""Synthetic source docstring."""\ndef normal(): pass')
    assert symbol.docline == "Synthetic source docstring."
    assert "Synthetic source" not in symbol.index_entry()
    assert "opaque body" not in extract("a.txt", "opaque body").index_entry()
    assert SECRET not in Symbols("a.py", "python", [SECRET]).index_entry()
    with pytest.raises(WorkspaceError):
        extract("a.py")


def workspace(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "nested" / "safe.py").write_text("def safe(): pass")
    outside = tmp_path / "outside.py"
    outside.write_text("OUTSIDE SYNTHETIC CONTENT")
    return root, outside, ApprovedWorkspace(root)


def test_root_must_be_independently_configured(tmp_path, monkeypatch):
    root, outside, _ = workspace(tmp_path)
    with pytest.raises(WorkspaceError):
        configured_workspace(root)
    monkeypatch.setenv("JEV_WORKSPACE_ROOTS", str(root))
    assert configured_workspace(root).read_text("nested/safe.py") == "def safe(): pass"
    with pytest.raises(WorkspaceError):
        configured_workspace(tmp_path)
    with pytest.raises(WorkspaceError):
        configured_workspace(root / "..")


@pytest.mark.parametrize("relative", ["../outside.py", "nested/../../outside.py", ".env", ".ssh/id_rsa", "file.pem", ".git/config"])
def test_denied_paths_rejected_before_read(tmp_path, relative):
    root, outside, ws = workspace(tmp_path)
    with pytest.raises(WorkspaceError):
        ws.read_text(relative)
    with pytest.raises(WorkspaceError):
        ws.read_text(outside)


def test_symlink_chain_internal_external_and_root_all_denied(tmp_path, monkeypatch):
    root, outside, ws = workspace(tmp_path)
    (root / "internal.py").symlink_to(root / "nested" / "safe.py")
    (root / "external.py").symlink_to(outside)
    (root / "chain.py").symlink_to(root / "external.py")
    for relative in ("internal.py", "external.py", "chain.py"):
        with pytest.raises(WorkspaceError):
            ws.read_text(relative)
    linkroot = tmp_path / "alias"
    linkroot.symlink_to(root, target_is_directory=True)
    with pytest.raises(WorkspaceError):
        ApprovedWorkspace(linkroot)
    assert ws.gather(["**/*.py"]) == [root / "nested" / "safe.py"]


def test_symlink_swap_at_actual_open_never_reads_outside(tmp_path, monkeypatch):
    root, outside, ws = workspace(tmp_path)
    original = os.open
    swapped = False
    def swapping(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "safe.py" and not swapped:
            swapped = True
            (root / "nested" / "safe.py").unlink()
            (root / "nested" / "safe.py").symlink_to(outside)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", swapping)
    with pytest.raises(WorkspaceError):
        ws.read_text("nested/safe.py")
    assert swapped


def test_intermediate_directory_swap_keeps_pinned_descriptor(tmp_path, monkeypatch):
    root, outside, ws = workspace(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "safe.py").write_text("OUTSIDE")
    original = os.open
    def swapping(path, flags, *args, **kwargs):
        if path == "safe.py":
            (root / "nested").rename(root / "old")
            (root / "nested").symlink_to(other, target_is_directory=True)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", swapping)
    assert ws.read_text("nested/safe.py") == "def safe(): pass"


def test_file_types_and_bounds(tmp_path):
    root, outside, ws = workspace(tmp_path)
    (root / "huge.py").write_bytes(b"x" * 1_000_001)
    (root / "binary.py").write_bytes(b"text\x00binary")
    (root / "invalid.py").write_bytes(b"\xff")
    (root / "controls.py").write_bytes(b"\x01\x02\x03")
    os.mkfifo(root / "fifo.py")
    os.link(outside, root / "hardlink.py")
    for name in ("huge.py", "binary.py", "invalid.py", "controls.py", "fifo.py", "hardlink.py", "nested"):
        with pytest.raises(WorkspaceError):
            ws.read_text(name)


def test_mcp_outline_and_gather_use_approved_boundary(tmp_path, monkeypatch):
    import mcp_server
    root, outside, _ = workspace(tmp_path)
    assert "unavailable" in mcp_server.jev_file_outline([str(outside)])[str(outside)]
    monkeypatch.setenv("JEV_WORKSPACE_ROOTS", str(root))
    paths = mcp_server._gather(root, None)
    assert paths == [root / "nested" / "safe.py"]
    result = mcp_server.jev_file_outline([str(root / "nested" / "safe.py"), str(outside)])
    assert "safe()" in result[str(root / "nested" / "safe.py")]
    assert "OUTSIDE" not in json.dumps(result)


@pytest.mark.parametrize("mode", ["default", "bypassPermissions"])
@pytest.mark.parametrize("verdict,expected", [("allow", None), ("review", "ask"), ("block", "ask"), ("unexpected", "ask")])
def test_enforcing_hook_never_emits_allow(mode, verdict, expected, monkeypatch):
    import hooks
    import permission_gate
    emitted = []
    monkeypatch.setattr(hooks, "_emit", emitted.append)
    monkeypatch.setattr(hooks, "_have_key", lambda: True)
    monkeypatch.setattr(permission_gate, "gate", lambda *args: {"final": verdict, "reason": SECRET})
    hooks.gate_bash({"permission_mode": mode, "tool_input": {"command": "git push origin main"}})
    assert [o["hookSpecificOutput"]["permissionDecision"] for o in emitted] == ([] if expected is None else [expected])
    assert SECRET not in json.dumps(emitted)


def test_deterministic_deny_cannot_be_overridden_by_model(monkeypatch):
    import hooks
    import permission_gate
    emitted = []
    monkeypatch.setattr(hooks, "_emit", emitted.append)
    monkeypatch.setattr(permission_gate, "gate", lambda *args: pytest.fail("Model must never see deterministic denial"))
    hooks.gate_bash({"permission_mode": "bypassPermissions", "tool_input": {"command": "rm -rf /"}})
    assert emitted[0]["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_error_requires_approval_without_exception_leak(monkeypatch, capsys):
    import hooks
    import permission_gate
    emitted = []
    monkeypatch.setattr(hooks, "_emit", emitted.append)
    monkeypatch.setattr(hooks, "_have_key", lambda: True)
    def failure(*args):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(permission_gate, "gate", failure)
    hooks.gate_bash({"tool_input": {"command": "git push origin main"}})
    assert emitted[0]["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert SECRET not in json.dumps(emitted) + str(capsys.readouterr())


def test_hook_updates_do_not_grant_host_permission(monkeypatch):
    import hooks
    emitted = []
    monkeypatch.setattr(hooks, "_emit", emitted.append)
    hooks.route_agent({"tool_input": {"prompt": "Find a definition", "model": "opus"}})
    out = emitted[0]["hookSpecificOutput"]
    assert "permissionDecision" not in out
    assert out["updatedInput"]["model"] == "opus"


@pytest.mark.parametrize("command", [
    "python /tmp/upload.py",
    "python -c 'import urllib.request; urllib.request.urlopen(\"https://example.test\")'",
    "python -m arbitrary_module",
    "bash /tmp/script.sh",
    "node -e 'fetch(\"https://example.test\")'",
    "bash -c 'python /tmp/upload.py'",
    "./helper", "/tmp/helper", "source ./env.sh", "perl -e 'print 1'",
    "python <<'PY'\nprint(1)\nPY",
    "uv run python /tmp/upload.py",
    "uv --directory /tmp run python upload.py",
    "uv run --with package python /tmp/upload.py",
    "uv tool run python /tmp/upload.py",
    "uvx --from package python /tmp/upload.py",
    "npx -y -p tsx tsx /tmp/upload.ts",
    "npm exec --package=tsx -- tsx /tmp/upload.ts",
    "pnpm --filter project exec node /tmp/upload.js",
    "pnpm dlx tsx /tmp/upload.ts",
    "yarn dlx tsx /tmp/upload.ts",
    "sudo -u operator env MODE=test uv run python /tmp/upload.py",
    "bash -c 'uv run python /tmp/upload.py'",
])
def test_opaque_execution_requires_approval_without_model(command, monkeypatch):
    import hooks
    import permission_gate
    emitted = []
    monkeypatch.setattr(hooks, "_emit", emitted.append)
    monkeypatch.setattr(permission_gate, "gate", lambda *args: pytest.fail("Opaque execution must not obtain model consent"))
    hooks.gate_bash({"permission_mode": "bypassPermissions", "tool_input": {"command": command}})
    assert emitted[0]["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_security_router_never_echoes_secret_reasons_or_inline_allowlist(monkeypatch):
    import security_router
    traces = []
    monkeypatch.setattr(security_router, "write_trace", lambda *args: traces.append(args))
    monkeypatch.setattr(security_router, "decide", lambda *args: pytest.fail("Secret must not reach model"))
    result = security_router.security_route("Explain file", ["src/a.py"], SECRET + " # pragma: allowlist secret")
    assert result["selected"] == "block"
    assert SECRET not in json.dumps(result) + json.dumps(traces)


def test_root_cannot_target_special_private_directories(tmp_path):
    denied = tmp_path / ".ssh"
    denied.mkdir()
    with pytest.raises(WorkspaceError):
        ApprovedWorkspace(denied)


def test_sensitivity_in_paths_and_symbols_is_screened_before_dispatch(monkeypatch):
    ct, seen, traces = fake_context(monkeypatch)
    results = []
    for path, body in [(PERSONAL + ".py", "def okay(): pass"), ("a.py", "def ghp_" + "A1b2" * 9 + "(): pass")]:
        results.append(ct.select("Find source", [ct.Chunk("one", body, path=path)]))
    assert not seen and not traces
    assert all(len(r.excluded) == 1 for r in results)


def test_hook_main_timeout_fallback_and_exception_are_approval(monkeypatch, capsys):
    import hooks
    import signal
    from io import StringIO
    captured = {}
    monkeypatch.setattr(signal, "signal", lambda signum, handler: captured.update(handler=handler))
    monkeypatch.setattr(signal, "alarm", lambda n: None)
    monkeypatch.setattr(hooks.os, "_exit", lambda n: (_ for _ in ()).throw(SystemExit(n)))
    hooks._arm_budget(hooks.FALLBACK["gate-bash"])
    with pytest.raises(SystemExit):
        captured["handler"](None, None)
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "ask"
    monkeypatch.setattr(hooks.sys, "stdin", StringIO('{"tool_input":{"command":"git push"}}'))
    def broken(data):
        raise RuntimeError(SECRET)
    monkeypatch.setitem(hooks.HANDLERS, "gate-bash", broken)
    hooks.main(["gate-bash"])
    output = capsys.readouterr()
    assert json.loads(output.out)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert SECRET not in output.out + output.err


@pytest.mark.parametrize("path,source,expected", [
    ("a.py", "__all__=['Unannounced acquisition briefing excerpt']\ndef safe(): pass", ["safe()"]),
    ("a.ts", "export function write({text = 'Unannounced acquisition briefing excerpt'} = {}) {}", ["write(text)"]),
    ("a.ts", "export const write = ({text = 'Unannounced acquisition briefing excerpt'} = {}) => {};", ["write(text)"]),
    ("a.ts", "export function write({privateName: renamed = 'Unannounced acquisition briefing excerpt', nested: {inner}, ...rest}, [item = 'Unannounced acquisition briefing excerpt']) {}", ["write(renamed, inner, rest, item)"]),
])
def test_symbol_metadata_never_embeds_literal_content(path, source, expected, monkeypatch):
    ct, seen, _ = fake_context(monkeypatch)
    ct.select("Find write", [ct.Chunk("file", source, path=path)])
    assert seen[0]["chunks"]["file"]["symbols"] == expected
    assert "Unannounced acquisition briefing excerpt" not in json.dumps(seen)


@pytest.mark.parametrize("personal", [PERSONAL, "123-45-6789", "diagnosis: synthetic-condition"])
def test_local_classifier_does_not_label_personal_data_public(personal):
    from security_router import CUSTOMER, classify
    label, reasons = classify([], personal, allow_inline_allowlist=False)
    assert label == CUSTOMER
    assert personal not in repr(reasons)


def test_trace_screening_includes_filename(tmp_path, monkeypatch):
    import core
    monkeypatch.setattr(core, "TRACE_DIR", tmp_path)
    path = core.write_trace(SECRET, {"value": SECRET}, {"reason": PERSONAL})
    assert SECRET not in str(path)
    stored = path.read_text()
    assert SECRET not in stored and PERSONAL not in stored


def test_invisible_formatting_cannot_bypass_known_sensitive_patterns():
    assert safe_metadata("src/\u202efake.py") == REDACTED
    with pytest.raises(PrivacyError):
        screen_outbound(SECRET[:8] + "\u200b" + SECRET[8:])


def test_root_swapped_after_validation_is_checked_again_at_read(tmp_path):
    root, outside, ws = workspace(tmp_path)
    root.rename(tmp_path / "old_root")
    root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WorkspaceError):
        ws.read_text("outside.py")


def test_growing_file_is_bounded_and_rejected(tmp_path, monkeypatch):
    root, outside, ws = workspace(tmp_path)
    original_read = os.read
    changed = False
    def growing(fd, size):
        nonlocal changed
        value = original_read(fd, size)
        if not changed:
            changed = True
            with (root / "nested" / "safe.py").open("a") as file:
                file.write("new content")
        return value
    monkeypatch.setattr(os, "read", growing)
    with pytest.raises(WorkspaceError):
        ws.read_text("nested/safe.py")
