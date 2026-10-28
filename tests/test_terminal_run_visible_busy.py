"""terminal_run mode='visible' never types into a program running in the
foreground of the visible terminal, and needs no terminal to be open.

Visible mode writes into the newest visible PTY. When another program held
its foreground (a dev server one chat left running), the command bytes went
to that program's stdin, the call ran to its timeout, and a Ctrl-C meant for
"this command" stopped the other program instead. Then it was refused; now it
runs in a new terminal tab. With no terminal open it used to fail after its
approval card (a field session lost two hours to that); now one is opened.
Either way the panel is asked to show the terminal (a terminal_show frame).
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


@pytest.fixture
def shown(monkeypatch):
    """The terminal_show frames published, and the terminals opened: a new
    one is a plain rc-free PTY, so the test does not run the user's login
    shell."""
    from server.agents.event_bus import event_bus

    frames: list[dict] = []
    opened: list = []
    real_publish = event_bus.publish

    def publish(session_id, event):
        if event.get("type") == "terminal_show":
            frames.append(event["terminalShow"])
            return None
        return real_publish(session_id, event)

    async def open_visible_session(cwd):
        session = t._create_pty_session(cwd, hidden=True)
        opened.append(session)
        return session

    monkeypatch.setattr(event_bus, "publish", publish)
    monkeypatch.setattr(t, "open_visible_session", open_visible_session)
    yield frames, opened
    for session in opened:
        session.kill()


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


def test_a_busy_terminal_gets_a_new_tab(monkeypatch, shown):
    frames, opened = shown

    async def body(s, shell_pgrp):
        os.write(s.master_fd, b"sleep 20\n")
        assert await _wait_for(lambda: t._foreground_pgrp(s) not in (None, shell_pgrp)), (
            "the probe program never reached the foreground"
        )
        ok, text = await do_terminal_run(
            {"command": "echo typed-elsewhere", "mode": "visible", "timeout": 10}
        )
        await asyncio.sleep(0.3)
        assert ok, text
        assert "typed-elsewhere" in text and "exit_code: 0" in text
        assert "typed-elsewhere" not in _buffer(s), "the command was written into the program"
        assert len(opened) == 1
        assert frames == [{"terminal_id": opened[0].id, "cwd": opened[0].cwd, "opened": True}]

    _with_visible_terminal(monkeypatch, body)


def test_runs_when_the_shell_is_at_its_prompt(monkeypatch, shown):
    frames, opened = shown

    async def body(s, shell_pgrp):
        ok, text = await do_terminal_run(
            {"command": "echo visible-ok", "mode": "visible", "timeout": 10}
        )
        assert ok, text
        assert "visible-ok" in text and "exit_code: 0" in text
        # The open terminal is used and shown, not a new one.
        assert opened == []
        assert frames == [{"terminal_id": s.id, "cwd": s.cwd, "opened": False}]

    _with_visible_terminal(monkeypatch, body)


def test_opens_a_terminal_when_none_is_open(monkeypatch, shown, tmp_path):
    frames, opened = shown
    monkeypatch.setattr(t, "latest_visible_session", lambda: None)
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: str(tmp_path))

    async def go():
        return await do_terminal_run({"command": "pwd", "mode": "visible", "timeout": 10})

    ok, text = asyncio.run(go())
    assert ok, text
    assert "No terminal was open" in text
    # It started in the workspace (pwd prints the real path of a symlinked tmp).
    assert any(p in text for p in (str(tmp_path), os.path.realpath(tmp_path)))
    assert len(opened) == 1 and opened[0].cwd == str(tmp_path)
    assert frames == [{"terminal_id": opened[0].id, "cwd": str(tmp_path), "opened": True}]


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


def test_the_new_tab_note_names_the_busy_program(monkeypatch, shown):
    async def body(s, shell_pgrp):
        os.write(s.master_fd, b"sleep 20\n")
        assert await _wait_for(lambda: t._foreground_pgrp(s) not in (None, shell_pgrp))
        ok, text = await do_terminal_run({"command": "echo x", "mode": "visible", "timeout": 10})
        assert ok, text
        assert "sleep is running" in text and "new terminal tab" in text

    _with_visible_terminal(monkeypatch, body)
