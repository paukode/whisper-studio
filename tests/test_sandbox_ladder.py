"""The sandbox mode ladder: read-only / workspace-write / danger-full-access.

Three properties under test:
  1. The readonly rung really is a rung — profile denies all writes except
     /dev (a PTY shell must write its own slave tty).
  2. Escalation is per-call consent: sandbox_permissions='danger-full-access'
     resolves to "ask" no matter what blanket grants exist, requires a
     justification, and is refused outright from unattended agents.
  3. Enforcement is a reported fact: a confined mode with no OS backend either
     fails closed (agents) or discloses (interactive), and a sandbox write
     denial in output is classified with an escalation hint.
"""

import asyncio
import os
import platform
import shutil
import tempfile

import pytest

from server import sandbox
from server.executors.terminal_run import do_terminal_run
from server.security.permissions import resolve_static_decision


def test_readonly_profile_denies_all_writes_except_dev(tmp_path):
    profile = sandbox._generate_macos_profile(str(tmp_path), write_mode="readonly")
    assert "(deny file-write*)" in profile
    assert '(allow file-write* (subpath "/dev"))' in profile
    # No workspace or temp allow-back — that's what separates it from workspace mode.
    assert f'(allow file-write* (subpath "{tmp_path}"))' not in profile
    assert '(allow file-write* (subpath "/private/tmp"))' not in profile


def test_readonly_rule_order_keeps_secret_denies_last(tmp_path):
    profile = sandbox._generate_macos_profile(str(tmp_path), write_mode="readonly")
    global_deny = profile.index("(deny file-write*)")
    dev_allow = profile.index('(allow file-write* (subpath "/dev"))')
    first_secret_deny = profile.index("(deny file-read* file-write*")
    assert global_deny < dev_allow < first_secret_deny


def test_escalation_always_resolves_to_ask():
    """No blanket grant may cover danger-full-access: not session approvals,
    not bypass mode, not a trusted skill."""
    tool_input = {
        "command": "touch ~/x",
        "sandbox_permissions": "danger-full-access",
        "justification": "test",
    }
    assert (
        resolve_static_decision(
            "terminal_run", tool_input, "cli", {"cli": "allow"}, "bypassPermissions", True
        )
        == "ask"
    )
    # An ordinary workspace-write call with the same grants resolves to allow.
    plain = {"command": "echo hi"}
    assert (
        resolve_static_decision("terminal_run", plain, "cli", {"cli": "allow"}, "default", False)
        == "allow"
    )


def test_danger_requires_justification():
    ok, msg = asyncio.run(
        do_terminal_run({"command": "echo hi", "sandbox_permissions": "danger-full-access"})
    )
    assert ok is False
    assert "justification" in msg


def test_danger_refused_for_unattended_agents():
    ok, msg = asyncio.run(
        do_terminal_run(
            {
                "command": "echo hi",
                "sandbox_permissions": "danger-full-access",
                "justification": "x",
                "__agent__": True,
            }
        )
    )
    assert ok is False
    assert "unattended" in msg


def test_invalid_permissions_value_is_refused():
    ok, msg = asyncio.run(do_terminal_run({"command": "echo hi", "sandbox_permissions": "yolo"}))
    assert ok is False
    assert "sandbox_permissions" in msg


def test_confined_agent_run_fails_closed_without_backend(monkeypatch):
    monkeypatch.setattr(sandbox, "pty_sandbox_backend", lambda: "none")

    async def fake_run_in_sandbox(command, cwd=None, timeout=None, write_mode="open"):
        raise AssertionError("must not run when failing closed")

    monkeypatch.setattr("server.terminal.run_in_sandbox", fake_run_in_sandbox)
    ok, msg = asyncio.run(do_terminal_run({"command": "echo hi", "__agent__": True}))
    assert ok is False
    assert "cannot be enforced" in msg


def test_confined_interactive_run_discloses_without_backend(monkeypatch):
    monkeypatch.setattr(sandbox, "pty_sandbox_backend", lambda: "none")
    seen = {}

    async def fake_run_in_sandbox(command, cwd=None, timeout=None, write_mode="open"):
        seen["write_mode"] = write_mode
        return {"exit_code": 0, "output": "ok", "timed_out": False}

    monkeypatch.setattr("server.terminal.run_in_sandbox", fake_run_in_sandbox)
    ok, out = asyncio.run(do_terminal_run({"command": "echo hi"}))
    assert ok is True
    assert seen["write_mode"] == "open"
    assert "[sandbox unavailable]" in out


def test_denial_classifier_names_mode_and_escalation(monkeypatch):
    monkeypatch.setattr(sandbox, "sandbox_backend", lambda: "sandbox-exec")
    hint = sandbox.classify_sandbox_denial(
        "touch: /Users/x/probe: Operation not permitted", "workspace"
    )
    assert hint is not None
    assert "workspace-write" in hint
    assert "danger-full-access" in hint
    # Open mode is never classified — the same string is an ordinary error there.
    assert sandbox.classify_sandbox_denial("Operation not permitted", "open") is None
    # Unrelated failures are not misattributed to the sandbox.
    assert sandbox.classify_sandbox_denial("No such file or directory", "workspace") is None


def test_denial_signatures_are_per_backend(monkeypatch):
    """bwrap's EROFS text must not be attributed to the sandbox on macOS."""
    monkeypatch.setattr(sandbox, "sandbox_backend", lambda: "sandbox-exec")
    assert sandbox.classify_sandbox_denial("Read-only file system", "workspace") is None
    monkeypatch.setattr(sandbox, "sandbox_backend", lambda: "bwrap")
    assert sandbox.classify_sandbox_denial("Read-only file system", "workspace") is not None


@pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("sandbox-exec") is None,
    reason="needs macOS sandbox-exec",
)
def test_readonly_blocks_workspace_writes_for_real():
    """Kernel check: readonly denies a write even INSIDE the working tree,
    where workspace mode would have allowed it."""
    ws = tempfile.mkdtemp(prefix="whisper-sbx-ro-")
    try:
        blocked = sandbox.run_sandboxed(f"touch {ws}/f", cwd=ws, timeout=20, write_mode="readonly")
        assert blocked.returncode != 0
        assert not os.path.exists(f"{ws}/f")

        read_ok = sandbox.run_sandboxed(f"ls {ws}", cwd=ws, timeout=20, write_mode="readonly")
        assert read_ok.returncode == 0
    finally:
        shutil.rmtree(ws, ignore_errors=True)
