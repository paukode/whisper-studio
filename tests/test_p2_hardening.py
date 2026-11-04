"""P2 hardening (defense in depth) from the write-route audit.

- login-persistence and shell startup files are on the sensitive deny-list.
- a confined command cannot reach the app's own control API port, so it
  cannot drive an approval to escape its write confinement.
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
    ):
        assert is_sensitive_path(os.path.expanduser("~/" + rel)), rel
    denied = {os.path.realpath(p) for p in expanded_sandbox_paths()}
    assert os.path.realpath(os.path.expanduser("~/Library/LaunchAgents")) in denied
    # An ordinary file is not caught.
    assert not is_sensitive_path(os.path.expanduser("~/Documents/notes.txt"))


def test_control_port_rule_reads_the_env(monkeypatch):
    from server.sandbox import _control_port_deny_rules

    monkeypatch.delenv("WHISPER_DEV_PROXY_PORT", raising=False)
    monkeypatch.setenv("PORT", "1234")
    assert _control_port_deny_rules() == ['(deny network-outbound (remote ip "localhost:1234"))']
    # Empty means the backend default port.
    monkeypatch.setenv("PORT", "")
    assert "41430" in _control_port_deny_rules()[0]
    # A non-numeric value adds no rule (no broken profile).
    monkeypatch.setenv("PORT", "nope")
    assert _control_port_deny_rules() == []


def test_the_dev_proxy_port_is_denied_too(monkeypatch):
    from server.sandbox import _control_port_deny_rules

    monkeypatch.setenv("PORT", "41430")
    monkeypatch.setenv("WHISPER_DEV_PROXY_PORT", "41460")
    assert _control_port_deny_rules() == [
        '(deny network-outbound (remote ip "localhost:41430"))',
        '(deny network-outbound (remote ip "localhost:41460"))',
    ]


def test_a_confined_profile_denies_the_control_port(monkeypatch):
    from server import sandbox

    monkeypatch.setenv("PORT", "8000")
    rule = '(deny network-outbound (remote ip "localhost:8000"))'
    for mode in ("workspace", "readonly"):
        assert rule in sandbox._generate_macos_profile("/tmp", write_mode=mode), mode
    # A human-approved (open) command keeps full loopback access.
    assert rule not in sandbox._generate_macos_profile("/tmp", write_mode="open")
