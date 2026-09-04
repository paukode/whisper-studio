"""Terminal readiness: send_and_wait returns as soon as a program is ready for
input (with a typed wait reason), and terminal_send drives a persistent
interactive PTY whose state survives across calls.

These use a real PTY (this platform has one). stdin_read is only positively
detectable on Linux (/proc/<pid>/wchan); on macOS the classifier honestly
reports inferred_idle, so the assertions below check the reasons that hold on
every platform: session_exit, fast idle return, and state persistence.
"""

import asyncio
import platform
import time

import pytest

from server import terminal as t
from server.executors.terminal_run import do_terminal_close, do_terminal_send

pytestmark = pytest.mark.skipif(
    platform.system() not in ("Darwin", "Linux"), reason="needs a POSIX PTY"
)


def test_send_returns_fast_when_a_repl_goes_idle():
    async def go():
        s = t._create_pty_session("/tmp", hidden=True)
        try:
            await asyncio.sleep(0.3)
            started = time.monotonic()
            r = await t.send_and_wait(s, "python3 -q\n", timeout=15)
            elapsed = time.monotonic() - started
            # The REPL settles well inside the budget — the whole point is that
            # we return on readiness, not on the timeout.
            assert elapsed < 10
            assert r["wait_reason"] in ("stdin_read", "inferred_idle")
            assert r["session_alive"]
        finally:
            s.kill()

    asyncio.run(go())


def test_expression_output_is_captured():
    async def go():
        s = t._create_pty_session("/tmp", hidden=True)
        try:
            await asyncio.sleep(0.3)
            await t.send_and_wait(s, "python3 -q\n", timeout=15)
            r = await t.send_and_wait(s, "2**32\n", timeout=15)
            assert "4294967296" in r["output"]
        finally:
            s.kill()

    asyncio.run(go())


def test_session_exit_is_detected():
    async def go():
        s = t._create_pty_session("/tmp", hidden=True)
        await asyncio.sleep(0.3)
        r = await t.send_and_wait(s, "exit\n", timeout=15)
        assert r["wait_reason"] == "session_exit"
        assert not r["session_alive"]

    asyncio.run(go())


def test_stdin_read_when_provable(monkeypatch):
    """When the OS positively reports a tty read, the reason is stdin_read (not
    the weaker inferred_idle). Forced here so it holds on every platform."""
    monkeypatch.setattr(t, "_blocked_on_tty_read", lambda _pgrp: True)

    async def go():
        s = t._create_pty_session("/tmp", hidden=True)
        try:
            await asyncio.sleep(0.3)
            r = await t.send_and_wait(s, "python3 -q\n", timeout=15)
            assert r["wait_reason"] == "stdin_read"
        finally:
            s.kill()

    asyncio.run(go())


def test_control_at_shell_prompt_is_idle_not_stdin_read(monkeypatch):
    """Even if the tty-read probe would say yes, control being back at the shell
    prompt (fg pgrp == shell pgrp) reports inferred_idle: the shell IS reading
    stdin, but 'ready for the next command' is the more useful fact."""
    monkeypatch.setattr(t, "_blocked_on_tty_read", lambda _pgrp: True)

    async def go():
        s = t._create_pty_session("/tmp", hidden=True)
        try:
            await asyncio.sleep(0.3)
            r = await t.send_and_wait(s, "echo hi\n", timeout=15)
            assert "hi" in r["output"]
            assert r["wait_reason"] == "inferred_idle"
        finally:
            s.kill()

    asyncio.run(go())


def test_interactive_tool_persists_state_across_calls():
    async def go():
        sid = "chat-persist-1"
        try:
            await do_terminal_send({"__session_id__": sid, "input": "python3 -q", "timeout": 15})
            await do_terminal_send({"__session_id__": sid, "input": "x = 21*2", "timeout": 15})
            ok, out = await do_terminal_send(
                {"__session_id__": sid, "input": "print(x)", "timeout": 15}
            )
            assert ok and "42" in out
            assert out.startswith("wait_reason:")
        finally:
            await do_terminal_close({"__session_id__": sid})

    asyncio.run(go())


def test_interactive_tool_validates_first_command_line():
    async def go():
        sid = "chat-validate-1"
        try:
            ok, msg = await do_terminal_send(
                {"__session_id__": sid, "input": "cat ~/.ssh/id_rsa", "timeout": 3}
            )
            assert ok is False and msg
        finally:
            await do_terminal_close({"__session_id__": sid})

    asyncio.run(go())


def test_close_is_idempotent():
    async def go():
        sid = "chat-close-1"
        await do_terminal_send({"__session_id__": sid, "input": "echo hi", "timeout": 10})
        ok1, _ = await do_terminal_close({"__session_id__": sid})
        ok2, msg2 = await do_terminal_close({"__session_id__": sid})
        assert ok1 is True
        assert ok2 is True and "No interactive terminal" in msg2

    asyncio.run(go())


def test_empty_input_reads_pending_output_without_error():
    async def go():
        sid = "chat-empty-1"
        try:
            await do_terminal_send({"__session_id__": sid, "input": "echo marker", "timeout": 10})
            ok, out = await do_terminal_send({"__session_id__": sid, "input": "", "timeout": 3})
            assert ok is True
        finally:
            await do_terminal_close({"__session_id__": sid})

    asyncio.run(go())
