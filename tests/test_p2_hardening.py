"""P2 hardening (defense in depth) from the write-route audit.

- login-persistence and shell startup files are on the sensitive deny-list.
- a confined command cannot reach the app's own control API port, so it
  cannot drive an approval to escape its write confinement.
- a voice turn reads its permission mode from permissions.json (the UI's
  source), not config permission_mode.
"""

from __future__ import annotations

import asyncio
import json
import os

from server.approval.markers import approval_request
from server.chat.engine.pause import paused_sessions
from server.exec.headless import run_headless_turn
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block


def test_login_persistence_paths_are_sensitive():
    from server.security.sensitive_paths import expanded_sandbox_paths, is_sensitive_path

    for rel in (
        ".zshrc",
        ".bashrc",
        ".profile",
        ".zprofile",
        "Library/LaunchAgents/com.evil.plist",
        ".config/autostart/evil.desktop",
    ):
        assert is_sensitive_path(os.path.expanduser("~/" + rel)), rel
    denied = {os.path.realpath(p) for p in expanded_sandbox_paths()}
    assert os.path.realpath(os.path.expanduser("~/Library/LaunchAgents")) in denied
    # An ordinary file is not caught.
    assert not is_sensitive_path(os.path.expanduser("~/Documents/notes.txt"))


def test_control_port_rule_reads_the_env(monkeypatch):
    from server.sandbox import _control_port_deny_rules

    monkeypatch.setenv("PORT", "1234")
    assert _control_port_deny_rules() == ['(deny network-outbound (remote ip "localhost:1234"))']
    # Empty falls back to the app's own default port.
    monkeypatch.setenv("PORT", "")
    assert "8000" in _control_port_deny_rules()[0]
    # A non-numeric value adds no rule (no broken profile).
    monkeypatch.setenv("PORT", "nope")
    assert _control_port_deny_rules() == []


def test_a_confined_profile_denies_the_control_port(monkeypatch):
    from server import sandbox

    monkeypatch.setenv("PORT", "8000")
    rule = '(deny network-outbound (remote ip "localhost:8000"))'
    for mode in ("workspace", "readonly"):
        assert rule in sandbox._generate_macos_profile("/tmp", write_mode=mode), mode
    # A human-approved (open) command keeps full loopback access.
    assert rule not in sandbox._generate_macos_profile("/tmp", write_mode="open")


# ── voice reads the mode from permissions.json ──────────────────────────────

_CFG = {
    "chat_models": {"sonnet": "test-model"},
    "default_chat_model": "sonnet",
    "permission_mode": "default",  # config says default; the UI mode is elsewhere
    "permission_explainer_enabled": False,
}


def test_voice_reads_the_mode_from_permissions_not_config(monkeypatch):
    from server.approval.bootstrap import register_defaults

    register_defaults()
    sid = "sess-voice-mode"
    paused_sessions.pop(f"exec:{sid}", None)

    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "ws_run_command", {"command": "echo hi"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("done"), *msg_end(stop_reason="end_turn")],
        ]
    )
    tools = [{"name": "ws_run_command", "description": "d", "input_schema": {"type": "object"}}]
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool", lambda *a, **k: (tools, [], 1)
    )
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    from server.infrastructure import config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: dict(_CFG))
    # The UI mode (permissions.json) is dontAsk: a command is denied with no
    # card. Config permission_mode is still "default" (would pause on a card).
    monkeypatch.setattr("server.security.permissions.get_mode", lambda: "dontAsk")

    async def fake_route_tool(tool_name, tool_input, **kw):
        payload = json.dumps({"action": "command", "command": tool_input["command"], "cwd": "/tmp"})
        return approval_request(payload), []

    monkeypatch.setattr("server.tool_executor.route_tool", fake_route_tool)

    events = asyncio.run(
        _collect(
            run_headless_turn(
                "run it", model_key="sonnet", ephemeral=True, session_id=sid, attended=True
            )
        )
    )
    types = [e["type"] for e in events]
    # dontAsk denied the command: no card. If voice had used config (default),
    # the turn would have paused with an approval_request.
    assert "approval_request" not in types
    assert events[-1]["status"] == "completed"


async def _collect(gen):
    return [event async for event in gen]
