"""terminal_run mode='visible' never types into a program running in the
foreground of the visible terminal.

Visible mode writes into the newest visible PTY. When another program held
its foreground (a dev server one chat left running), the command bytes went
to that program's stdin, the call ran to its timeout, and a Ctrl-C meant for
"this command" stopped the other program instead.
"""

import asyncio
import os
import platform
import time

import pytest

from server import terminal as t
from server.executors.terminal_run import do_terminal_run

pytestmark = pytest.mark.skipif(
    platform.system() not in ("Darwin", "Linux"), reason="needs a POSIX PTY"
)


def _buffer(session) -> str:
    with session.output_lock:
        return bytes(session.output_buffer).decode("utf-8", errors="replace")


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


def _with_visible_terminal(monkeypatch, body):
    """Run ``body(session)`` against a real PTY standing in for the user's open
    terminal tab (created inside the loop, which its reader task needs)."""

    async def go():
        session = t._create_pty_session("/tmp", hidden=True)
        monkeypatch.setattr(t, "latest_visible_session", lambda: session)
        try:
            shell_pgrp = os.getpgid(session.process.pid)
            assert await _wait_for(lambda: t._foreground_pgrp(session) == shell_pgrp)
            await body(session, shell_pgrp)
        finally:
            session.kill()

    asyncio.run(go())


def test_refuses_while_another_program_holds_the_foreground(monkeypatch):
    async def body(s, shell_pgrp):
        os.write(s.master_fd, b"sleep 20\n")
        assert await _wait_for(lambda: t._foreground_pgrp(s) not in (None, shell_pgrp)), (
            "the probe program never reached the foreground"
        )
        ok, text = await do_terminal_run(
            {"command": "echo typed-into-it", "mode": "visible", "timeout": 3}
        )
        await asyncio.sleep(0.3)
        assert not ok
        assert "busy" in text
        assert "typed-into-it" not in _buffer(s), "the command was written into the program"

    _with_visible_terminal(monkeypatch, body)


def test_runs_when_the_shell_is_at_its_prompt(monkeypatch):
    async def body(s, shell_pgrp):
        ok, text = await do_terminal_run(
            {"command": "echo visible-ok", "mode": "visible", "timeout": 10}
        )
        assert ok, text
        assert "visible-ok" in text and "exit_code: 0" in text

    _with_visible_terminal(monkeypatch, body)


def test_runs_at_the_prompt_of_a_nested_shell(monkeypatch):
    """A nested interactive shell (bash, nix develop, a tmux or ssh prompt)
    holds the foreground in a group of its own, but a command typed there runs
    at its prompt and completes as usual, so it is not busy."""

    async def body(s, shell_pgrp):
        os.write(s.master_fd, b"/bin/bash --noprofile --norc -i\n")
        assert await _wait_for(lambda: t._foreground_pgrp(s) not in (None, shell_pgrp)), (
            "the nested shell never took the foreground"
        )
        ok, text = await do_terminal_run(
            {"command": "echo nested-ok", "mode": "visible", "timeout": 10}
        )
        assert ok, text
        assert "nested-ok" in text and "exit_code: 0" in text

    _with_visible_terminal(monkeypatch, body)


def test_busy_refusal_names_the_program(monkeypatch):
    async def body(s, shell_pgrp):
        os.write(s.master_fd, b"sleep 20\n")
        assert await _wait_for(lambda: t._foreground_pgrp(s) not in (None, shell_pgrp))
        ok, text = await do_terminal_run({"command": "echo x", "mode": "visible", "timeout": 3})
        assert not ok
        assert "sleep is running" in text

    _with_visible_terminal(monkeypatch, body)
