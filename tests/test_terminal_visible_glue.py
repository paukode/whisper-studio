"""A command typed into a terminal never glues onto other input.

Field report: a 1460-byte command was typed into the visible terminal while
an earlier visible run (``bash script.sh``) still held its foreground. The
tty was in canonical mode, so the line was cut at MAX_CANON (1024 bytes),
lost its newline, and the next command was appended to it: zsh was left at
"pipe pipe quote>". A foreground ``bash`` counted as an idle prompt, so the
run was typed in at all.

Now a long or multi-line command is sourced from a script file through one
short line, every payload starts with Ctrl-U, a shell that runs a script is
busy, and the assistant's own timed-out run refuses further visible runs
until it ends (terminal_send target='visible' sends it Ctrl-C).
"""

import asyncio
import os
import tempfile
import time

import pytest

from server import terminal as t
from server.executors.terminal_run import _foreground_program, do_terminal_run, do_terminal_send


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


def _in_terminal(body, monkeypatch=None):
    """Run ``body(session, shell_pgrp)`` against a real rc-free PTY (made in
    the loop, which its reader task needs); with ``monkeypatch`` it also
    stands in for the user's open visible terminal."""

    async def go():
        session = t._create_pty_session("/tmp", hidden=True)
        if monkeypatch is not None:
            monkeypatch.setattr(t, "latest_visible_session", lambda: session)
        try:
            shell_pgrp = os.getpgid(session.process.pid)
            assert await _wait_for(lambda: t._foreground_pgrp(session) == shell_pgrp)
            await asyncio.sleep(0.3)
            await body(session, shell_pgrp)
        finally:
            session.kill()

    asyncio.run(go())


def _long_command(size: int = 1500) -> str:
    filler = "x" * (size - 20)
    command = f"echo START; : '{filler}'; echo END"
    assert len(command.encode()) >= size
    return command


def test_a_long_command_runs_in_an_idle_shell():
    async def body(s, shell_pgrp):
        result = await t.run_in_session(s, _long_command(), timeout=10)
        assert result["timed_out"] is False, result
        assert result["exit_code"] == 0
        out = result["output"]
        assert "START" in out and "END" in out
        # Only the command's output: no echoed source line or marker printf.
        assert "terminal-run-" not in out and "__CLAUDE_DONE_" not in out
        # The script it was sourced from is gone.
        assert os.listdir(t.command_script_dir()) == []

    _in_terminal(body)


def test_a_multi_line_command_runs_and_keeps_its_exit_code():
    async def body(s, shell_pgrp):
        command = "echo one\n# a comment line\necho two\nfalse"
        result = await t.run_in_session(s, command, timeout=10)
        assert result["timed_out"] is False, result
        assert result["exit_code"] == 1
        assert result["output"].split() == ["one", "two"]

    _in_terminal(body)


def test_the_script_file_is_owner_only():
    path = t._write_command_script("echo hi", "abcd1234abcd1234")
    try:
        assert os.stat(path).st_mode & 0o777 == 0o700
        assert os.stat(os.path.dirname(path)).st_mode & 0o777 == 0o700
        assert open(path).read().endswith("\necho hi\n")
    finally:
        t._remove_script(path)
    assert not os.path.exists(path)


def test_a_short_one_line_command_is_typed_directly():
    payload = t._build_run_payload("echo hi", "aa11bb22")
    assert payload.startswith("\x15echo hi\n")
    assert t._needs_script("echo hi") is False
    assert t._needs_script("echo a\necho b") is True
    assert t._needs_script("x" * (t._INLINE_COMMAND_MAX + 1)) is True


def test_leftover_partial_input_is_cleared():
    async def body(s, shell_pgrp):
        # Half-typed input with an open quote, never finished with Enter.
        os.write(s.master_fd, b"echo partial-junk 'unclosed")
        await asyncio.sleep(0.3)
        result = await t.run_in_session(s, "echo clean-ok", timeout=10)
        assert result["timed_out"] is False, result
        assert result["exit_code"] == 0
        assert result["output"].strip() == "clean-ok"

    _in_terminal(body)


def test_the_echoed_marker_printf_is_dropped():
    marker = "0011223344556677"
    raw = (
        "echo hi\r\n"
        f"printf '\\n__CLAUDE_DONE_{marker}:%d\\n' $?\r\n"
        "hi\r\n"
        f"\r\n__CLAUDE_DONE_{marker}:0\r\n$ "
    )
    assert t._clean_command_output(raw, "echo hi", marker) == "hi"


def _script(tmp_path, body: str) -> str:
    path = tmp_path / "job.sh"
    path.write_text(body)
    return str(path)


def test_a_shell_running_a_script_is_busy(tmp_path):
    job = _script(tmp_path, "sleep 20\n")

    async def body(s, shell_pgrp):
        os.write(s.master_fd, f"/bin/bash --noprofile --norc {job}\n".encode())
        assert await _wait_for(lambda: t._foreground_pgrp(s) not in (None, shell_pgrp))
        assert await asyncio.to_thread(_foreground_program, s) == "bash"

    _in_terminal(body)


def test_a_script_whose_child_reads_raw_keys_is_busy(tmp_path):
    """A script that runs a pager, a REPL or fzf: the child turns canonical
    mode off, but it runs in the script's group, so the terminal is busy."""
    job = _script(
        tmp_path,
        "exec 3<&0\n/usr/bin/python3 -c 'import tty,time; tty.setraw(0); time.sleep(20)' <&3\n",
    )

    async def body(s, shell_pgrp):
        from server.executors.terminal_run import _line_editor_reading

        os.write(s.master_fd, f"/bin/bash --noprofile --norc {job}\n".encode())
        assert await _wait_for(
            lambda: t._foreground_pgrp(s) not in (None, shell_pgrp) and _line_editor_reading(s),
            timeout=8,
        )
        # Give the child time to take the keys (raw mode) before the check.
        await asyncio.sleep(0.5)
        assert _line_editor_reading(s)
        assert await asyncio.to_thread(_foreground_program, s) == "bash"

    _in_terminal(body)


def test_scripts_live_where_a_sandboxed_command_cannot_write(tmp_path):
    """The visible shell sources the script unconfined, so it is not in the
    scratch folder or temp: the OS sandbox denies writes to its folder even
    for a workspace that holds the data root."""
    from server.infrastructure.paths import data_root
    from server.sandbox import _generate_macos_profile

    folder = t.command_script_dir()
    assert not folder.startswith(tempfile.gettempdir())
    profile = _generate_macos_profile(
        data_root(), write_mode="workspace", scratch_dir=str(tmp_path)
    )
    deny = f'(deny file-write* (subpath "{folder}"))'
    allow = f'(allow file-write* (subpath "{os.path.realpath(data_root())}"))'
    assert deny in profile
    assert profile.index(deny) > profile.index(allow)


def test_a_nested_shell_at_its_prompt_is_idle():
    async def body(s, shell_pgrp):
        os.write(s.master_fd, b"/bin/bash --noprofile --norc -i\n")
        assert await _wait_for(lambda: t._foreground_pgrp(s) not in (None, shell_pgrp))
        idle = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not idle:
            idle = await asyncio.to_thread(_foreground_program, s) is None
            await asyncio.sleep(0.1)
        assert idle, "a nested interactive shell at its prompt counted as busy"

    _in_terminal(body)


@pytest.fixture
def no_new_tabs(monkeypatch):
    """Fail the test if a run opens a new visible terminal, and drop the
    terminal_show frames."""
    from server.agents.event_bus import event_bus

    opened: list = []

    async def open_visible_session(cwd):
        opened.append(cwd)
        raise AssertionError("a new terminal tab was opened")

    monkeypatch.setattr(t, "open_visible_session", open_visible_session)
    monkeypatch.setattr(event_bus, "publish", lambda *a, **k: None)
    return opened


def test_a_timed_out_visible_run_refuses_further_runs_until_stopped(
    monkeypatch, tmp_path, no_new_tabs
):
    job = _script(tmp_path, "echo job-started\nsleep 30\n")

    async def body(s, shell_pgrp):
        ok, text = await do_terminal_run(
            {"command": f"/bin/bash --noprofile --norc {job}", "mode": "visible", "timeout": 1}
        )
        assert ok, text
        assert "TIMED OUT" in text
        assert "still running in the visible terminal" in text
        assert "refused until it ends" in text
        assert "terminal_send" in text and "target='visible'" in text
        assert s.pending_run is not None

        # A further visible run is refused cleanly: nothing is typed in.
        before = _buffer(s)
        ok, text = await do_terminal_run(
            {"command": "echo must-not-type " + "y" * 1400, "mode": "visible", "timeout": 5}
        )
        assert not ok
        assert "still running" in text and "\\x03" in text
        await asyncio.sleep(0.3)
        assert "must-not-type" not in _buffer(s)[len(before) :]

        # Ctrl-C through terminal_send target='visible' stops it.
        ok, text = await do_terminal_send(
            {"session_id": "s1", "input": "\x03", "target": "visible", "timeout": 2}
        )
        assert ok, text
        assert await _wait_for(lambda: t._foreground_pgrp(s) == shell_pgrp)

        # The terminal is usable again, in place.
        ok, text = await do_terminal_run(
            {"command": "echo after-stop", "mode": "visible", "timeout": 10}
        )
        assert ok, text
        assert "after-stop" in text and "exit_code: 0" in text
        assert s.pending_run is None
        assert no_new_tabs == []

    _in_terminal(body, monkeypatch)


def test_visible_target_sends_only_ctrl_c(monkeypatch):
    monkeypatch.setattr(t, "latest_visible_session", lambda: None)

    async def go():
        typed = await do_terminal_send(
            {"session_id": "s1", "input": "rm -rf build", "target": "visible"}
        )
        eof = await do_terminal_send({"session_id": "s1", "input": "\x04", "target": "visible"})
        none_open = await do_terminal_send(
            {"session_id": "s1", "input": "\x03", "target": "visible"}
        )
        bad = await do_terminal_send({"session_id": "s1", "input": "\x03", "target": "other"})
        return typed, eof, none_open, bad

    typed, eof, none_open, bad = asyncio.run(go())
    assert typed[0] is False and "only" in typed[1]
    assert eof[0] is False and "only" in eof[1]
    assert none_open == (False, "No visible terminal is open.")
    assert bad[0] is False and "invalid target" in bad[1]


def test_visible_target_refuses_an_unattended_agent(monkeypatch):
    def no_terminal():
        raise AssertionError("an agent call looked up the visible terminal")

    monkeypatch.setattr(t, "latest_visible_session", no_terminal)
    ok, text = asyncio.run(
        do_terminal_send(
            {"session_id": "s1", "input": "\x03", "target": "visible", "__agent__": True}
        )
    )
    assert not ok and "unattended subagent" in text


def test_visible_target_never_interrupts_the_users_own_program(monkeypatch, tmp_path):
    """With no timed-out run of the assistant, Ctrl-C is not sent: the
    foreground program is the user's own (a dev server, say)."""
    job = _script(tmp_path, "echo user-job\nsleep 20\n")

    async def body(s, shell_pgrp):
        os.write(s.master_fd, f"/bin/bash --noprofile --norc {job}\n".encode())
        assert await _wait_for(lambda: "user-job" in _buffer(s))
        assert s.pending_run is None
        ok, text = await do_terminal_send(
            {"session_id": "s1", "input": "\x03", "target": "visible", "timeout": 1}
        )
        assert not ok and "No command of yours" in text
        await asyncio.sleep(0.3)
        assert t._foreground_pgrp(s) != shell_pgrp, "the user's program was stopped"

    _in_terminal(body, monkeypatch)
