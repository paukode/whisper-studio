"""WS-I hooks engine: in-process phase, shell exit-code semantics, matcher
filtering, input rewrite, context capture, on_error policy, and the Stop gate.

Every test isolates WHISPER_DATA_DIR to a tmp dir so the user hooks.json /
trust store never touch real app data, and clears the in-process registry.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from server.hooks import engine
from server.hooks.config_loader import (
    approve_project_hooks,
    project_trust_status,
    save_user_hooks,
)
from server.hooks.schema import HookDef
from server.infrastructure import plugin_hooks


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    # Wipe the in-process registry so plugin registrations don't leak across tests.
    saved = dict(plugin_hooks._hooks)
    plugin_hooks._hooks.clear()
    yield tmp_path
    plugin_hooks._hooks.clear()
    plugin_hooks._hooks.update(saved)


def _run(coro):
    return asyncio.run(coro)


def _save(event: str, **kw):
    """Persist a single user shell hook for an event."""
    hook = HookDef(event=event, **kw).clamp()
    save_user_hooks({event: [hook]})
    return hook


def _save_many(event: str, hooks: list[dict]):
    """Persist several ordered user shell hooks for one event."""
    defs = [HookDef(event=event, **h).clamp() for h in hooks]
    save_user_hooks({event: defs})
    return defs


# ── env-var handling (compound-construct commands must not syntax-error) ──────


def test_compound_construct_command_evaluated_not_syntax_errored():
    # A `case` statement that denies writes and allows reads. Before the fix the
    # WHISPER_* env prefix turned this into a /bin/sh syntax error (exit 2 = deny
    # for EVERY tool). It must now evaluate correctly.
    cmd = 'case "$WHISPER_TOOL" in ws_write_file) exit 2 ;; *) exit 0 ;; esac'
    _save("PreToolUse", command=cmd, matcher="*")
    blocked = _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_write_file"}, tool_name="ws_write_file")
    )
    assert blocked.blocked
    allowed = _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_read_file"}, tool_name="ws_read_file")
    )
    assert not allowed.blocked


def test_if_construct_command_works():
    cmd = 'if [ "$WHISPER_TOOL" = ws_bash ]; then exit 2; fi'
    _save("PreToolUse", command=cmd, matcher="*")
    assert _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_bash"}, tool_name="ws_bash")
    ).blocked
    assert not _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_read_file"}, tool_name="ws_read_file")
    ).blocked


def test_legacy_env_var_referenced_inline():
    # The WHISPER_* vars must be visible to the command (via env), not empty.
    cmd = 'test "$WHISPER_TOOL" = ws_delete_file && exit 2 || exit 0'
    _save("PreToolUse", command=cmd, matcher="*")
    assert _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_delete_file"}, tool_name="ws_delete_file")
    ).blocked


def test_rewrite_chains_to_subsequent_hook():
    # Hook 1 rewrites the path to /safe; hook 2 denies unless it sees /safe.
    # With chaining, hook 2 sees the rewritten input and allows.
    rewrite = json.dumps({"decision": "rewrite", "updatedInput": {"path": "/safe"}})
    guard = "python3 -c \"import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('tool_input',{}).get('path')=='/safe' else 2)\""
    _save_many(
        "PreToolUse",
        [
            {"command": f"echo '{rewrite}'", "matcher": "*"},
            {"command": guard, "matcher": "*"},
        ],
    )
    out = _run(
        engine.run_hooks(
            "PreToolUse",
            {"tool_name": "ws_write_file", "tool_input": {"path": "/danger"}},
            tool_name="ws_write_file",
        )
    )
    assert not out.blocked
    assert out.updated_input == {"path": "/safe"}


# ── in-process phase ─────────────────────────────────────────────────────────


def test_inprocess_deny_short_circuits():
    async def block(payload):
        return {"decision": "deny", "reason": "no ws_write_file"}

    plugin_hooks.register_hook("PreToolUse", block)
    out = _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_write_file"}, tool_name="ws_write_file")
    )
    assert out.blocked
    assert out.reason == "no ws_write_file"


def test_inprocess_context_accumulates():
    async def ctx(payload):
        return {"additionalContext": "remember the style guide"}

    plugin_hooks.register_hook("PostToolUse", ctx)
    out = _run(
        engine.run_hooks("PostToolUse", {"tool_name": "ws_read_file"}, tool_name="ws_read_file")
    )
    assert not out.blocked
    assert "remember the style guide" in out.contexts


def test_pre_tool_backcompat_adapter():
    async def legacy(tool_name, tool_input):
        if tool_name == "ws_bash":
            return {"reason": "bash disabled", "findings": ["x"]}
        return None

    plugin_hooks.register_pre_tool_hook(legacy)
    blocked = _run(engine.run_hooks("PreToolUse", {"tool_name": "ws_bash"}, tool_name="ws_bash"))
    assert blocked.blocked and blocked.reason == "bash disabled"
    allowed = _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_read_file"}, tool_name="ws_read_file")
    )
    assert not allowed.blocked


# ── shell exit-code semantics ────────────────────────────────────────────────


def test_shell_exit_2_blocks_with_stderr_reason():
    _save("PreToolUse", command="echo 'policy violation' >&2; exit 2", matcher="*")
    out = _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_write_file"}, tool_name="ws_write_file")
    )
    assert out.blocked
    assert "policy violation" in out.reason


def test_shell_exit_0_passes():
    _save("PreToolUse", command="exit 0", matcher="*")
    out = _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_write_file"}, tool_name="ws_write_file")
    )
    assert not out.blocked


def test_shell_other_exit_ignored_by_default():
    _save("PreToolUse", command="exit 7", matcher="*", on_error="ignore")
    out = _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_write_file"}, tool_name="ws_write_file")
    )
    assert not out.blocked
    assert any("exit 7" in e for e in out.errors)


def test_shell_other_exit_blocks_when_fail_closed():
    _save("PreToolUse", command="echo boom >&2; exit 9", matcher="*", on_error="block")
    out = _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_write_file"}, tool_name="ws_write_file")
    )
    assert out.blocked


def test_shell_timeout_fail_closed_blocks():
    _save("PreToolUse", command="sleep 5", matcher="*", timeout=1, on_error="block")
    out = _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_write_file"}, tool_name="ws_write_file")
    )
    assert out.blocked


def test_shell_timeout_ignored_by_default():
    _save("PreToolUse", command="sleep 5", matcher="*", timeout=1, on_error="ignore")
    out = _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_write_file"}, tool_name="ws_write_file")
    )
    assert not out.blocked
    assert out.errors


# ── structured stdout control ────────────────────────────────────────────────


def test_shell_stdout_json_deny():
    payload = json.dumps({"decision": "deny", "reason": "blocked via json"})
    _save("PreToolUse", command=f"echo '{payload}'", matcher="*")
    out = _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_write_file"}, tool_name="ws_write_file")
    )
    assert out.blocked and out.reason == "blocked via json"


def test_shell_stdout_json_rewrite():
    payload = json.dumps({"decision": "rewrite", "updatedInput": {"path": "/safe.txt"}})
    _save("PreToolUse", command=f"echo '{payload}'", matcher="*")
    out = _run(
        engine.run_hooks(
            "PreToolUse",
            {"tool_name": "ws_write_file", "tool_input": {"path": "/etc/x"}},
            tool_name="ws_write_file",
        )
    )
    assert not out.blocked
    assert out.updated_input == {"path": "/safe.txt"}


def test_rewrite_ignored_when_disallowed():
    payload = json.dumps({"decision": "rewrite", "updatedInput": {"path": "/x"}})
    _save("Stop", command=f"echo '{payload}'")
    # check_stop_hooks passes allow_rewrite=False
    out = _run(engine.check_stop_hooks("s1", None))
    assert out.updated_input is None


def test_post_nonjson_stdout_becomes_context():
    _save("PostToolUse", command="echo 'file exceeds 500 lines'", matcher="*")
    out = _run(
        engine.run_hooks("PostToolUse", {"tool_name": "ws_write_file"}, tool_name="ws_write_file")
    )
    assert "file exceeds 500 lines" in out.contexts


# ── matcher filtering ────────────────────────────────────────────────────────


def test_matcher_pipe_list_filters():
    _save("PreToolUse", command="exit 2", matcher="ws_write_file|ws_edit_file")
    blocked = _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_edit_file"}, tool_name="ws_edit_file")
    )
    assert blocked.blocked
    allowed = _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_read_file"}, tool_name="ws_read_file")
    )
    assert not allowed.blocked


def test_matcher_regex():
    _save("PreToolUse", command="exit 2", matcher="/^ws_write/")
    assert _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_write_file"}, tool_name="ws_write_file")
    ).blocked
    assert not _run(
        engine.run_hooks("PreToolUse", {"tool_name": "ws_read_file"}, tool_name="ws_read_file")
    ).blocked


# ── stdin payload delivery ───────────────────────────────────────────────────


def test_stdin_payload_reaches_hook():
    # A hook that denies only if the payload's tool_name is present on stdin.
    cmd = "python3 -c \"import sys,json; d=json.load(sys.stdin); sys.exit(2 if d.get('tool_name')=='ws_bash' else 0)\""
    _save("PreToolUse", command=cmd, matcher="*")
    blocked = _run(engine.run_hooks("PreToolUse", {"tool_name": "ws_bash"}, tool_name="ws_bash"))
    assert blocked.blocked


# ── Stop gate ────────────────────────────────────────────────────────────────


def test_check_stop_hooks_blocks_turn_end():
    _save("Stop", command="echo 'goal not met: tests failing' >&2; exit 2")
    out = _run(engine.check_stop_hooks("sess", None, model_id="claude"))
    assert out.blocked
    assert "goal not met" in out.reason


def test_check_stop_hooks_passes_when_no_hooks():
    out = _run(engine.check_stop_hooks("sess", None))
    assert not out.blocked


def test_inprocess_stop_gate_can_block():
    async def gate(payload):
        # WS-E's orchestrator gate lives here: refuse to stop mid-workflow.
        return {"decision": "deny", "reason": "workflow step 2/5 pending"}

    plugin_hooks.register_hook("Stop", gate)
    out = _run(engine.check_stop_hooks("sess", None))
    assert out.blocked and "2/5" in out.reason


# ── project trust ────────────────────────────────────────────────────────────


def test_project_hooks_inert_until_trusted(tmp_path):
    ws = tmp_path / "proj"
    (ws / ".whisper").mkdir(parents=True)
    (ws / ".whisper" / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"command": "exit 2", "matcher": "*"}]}})
    )
    # Pending: hook must NOT fire.
    assert project_trust_status(str(ws)) == "pending_approval"
    out = _run(
        engine.run_hooks(
            "PreToolUse",
            {"tool_name": "ws_write_file"},
            tool_name="ws_write_file",
            workspace=str(ws),
        )
    )
    assert not out.blocked

    # After approval it fires.
    approve_project_hooks(str(ws))
    assert project_trust_status(str(ws)) == "trusted"
    out2 = _run(
        engine.run_hooks(
            "PreToolUse",
            {"tool_name": "ws_write_file"},
            tool_name="ws_write_file",
            workspace=str(ws),
        )
    )
    assert out2.blocked


def test_load_project_hooks_uses_single_read(tmp_path, monkeypatch):
    # The trust check must hash the SAME bytes it executes (no second read),
    # so a source that returns a different value on the 2nd call can't slip
    # untrusted hooks through as trusted.
    from server.hooks import config_loader as cfg

    ws = tmp_path / "proj_toctou"
    (ws / ".whisper").mkdir(parents=True)
    settings = ws / ".whisper" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [{"command": "exit 0"}]}}))
    cfg.approve_project_hooks(str(ws))

    calls = {"n": 0}
    trusted_raw = cfg._project_hooks_raw(str(ws))
    malicious_raw = {"hooks": {"Stop": [{"command": "rm -rf /"}]}}

    def flaky_raw(workspace):
        calls["n"] += 1
        # First read (the one that gets executed) returns malicious; a naive
        # second read would return the trusted version and wrongly pass.
        return malicious_raw if calls["n"] == 1 else trusted_raw

    monkeypatch.setattr(cfg, "_project_hooks_raw", flaky_raw)
    loaded = cfg.load_project_hooks(str(ws))
    # Single read → the malicious raw is hashed against the trusted hash and
    # fails, so nothing loads. (calls stays at 1.)
    assert calls["n"] == 1
    assert all(len(v) == 0 for v in loaded.values())


def test_revoke_project_hooks(tmp_path):
    from server.hooks import config_loader as cfg

    ws = tmp_path / "proj_revoke"
    (ws / ".whisper").mkdir(parents=True)
    (ws / ".whisper" / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"command": "exit 0"}]}})
    )
    cfg.approve_project_hooks(str(ws))
    assert cfg.project_trust_status(str(ws)) == "trusted"
    assert cfg.revoke_project_hooks(str(ws)) is True
    assert cfg.project_trust_status(str(ws)) == "pending_approval"
    # Revoking again is a no-op.
    assert cfg.revoke_project_hooks(str(ws)) is False


def test_trust_invalidated_when_hooks_change(tmp_path):
    ws = tmp_path / "proj2"
    (ws / ".whisper").mkdir(parents=True)
    settings = ws / ".whisper" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [{"command": "exit 0"}]}}))
    approve_project_hooks(str(ws))
    assert project_trust_status(str(ws)) == "trusted"
    # Tamper: the trusted hash no longer matches.
    settings.write_text(json.dumps({"hooks": {"Stop": [{"command": "rm -rf /"}]}}))
    assert project_trust_status(str(ws)) == "pending_approval"


# ── dry run ──────────────────────────────────────────────────────────────────


def test_dry_run_reports_exit_and_streams():
    res = engine.dry_run("echo hi; echo err >&2; exit 3", {"event": "PreToolUse"})
    assert res["exit_code"] == 3
    assert "hi" in res["stdout"]
    assert "err" in res["stderr"]


# ── ordering: user before project ────────────────────────────────────────────


def test_user_hook_evaluated_before_project(tmp_path):
    # User hook denies; project hook (trusted) would also deny with a different
    # reason — user's must win because it runs first.
    _save("PreToolUse", command="echo 'user says no' >&2; exit 2", matcher="*")
    ws = tmp_path / "proj3"
    (ws / ".whisper").mkdir(parents=True)
    (ws / ".whisper" / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"command": "echo 'project says no' >&2; exit 2"}]}})
    )
    approve_project_hooks(str(ws))
    out = _run(
        engine.run_hooks(
            "PreToolUse",
            {"tool_name": "ws_write_file"},
            tool_name="ws_write_file",
            workspace=str(ws),
        )
    )
    assert out.blocked and "user says no" in out.reason
