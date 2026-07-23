"""Regression tests for four LOW-severity audit fixes.

(1) server.lsp._python_diagnostics — a missing pyflakes must fall through to
    the py_compile fallback, not report "No module named pyflakes" as a file
    diagnostic.
(2) server.tool_executor.process_tool_results — on an approval pause, a sibling
    tool that ALREADY executed keeps its real result; only genuinely
    unexecuted tools (and the tool awaiting approval) get "[Not executed]".
(3) server.terminal._PtySession.kill — signals shutdown and lets the reader own
    closing master_fd (no close-under-read fd-reuse race). Reader closes the fd
    once it sees the _closing flag.
(4) server.tasks_tracker.update_task — an explicitly invalid status returns None
    without bumping updated_at; execute_task_tool surfaces an explicit error.
"""

import asyncio
import json
import os
import signal
from types import SimpleNamespace

import pytest

import server.lsp as lsp
import server.tasks_tracker as tt
import server.terminal as T
from server.approval.bootstrap import register_defaults
from server.security.permissions import MODE_DEFAULT
from server.tool_executor import process_tool_results

register_defaults()


# ── Fix (1): pyflakes-unavailable fall-through ───────────────────────────────


def test_pyflakes_unavailable_falls_through_to_py_compile(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "pyflakes" in cmd:
            # How a missing pyflakes actually surfaces: non-zero exit + an
            # import error on stderr (NOT a lint finding about the file).
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="/usr/bin/python3: No module named pyflakes",
            )
        if "py_compile" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(lsp.subprocess, "run", fake_run)
    out = lsp._python_diagnostics("/tmp/whatever.py")

    # The import error must NOT leak out as a diagnostic; we fell through to the
    # syntax-only fallback instead.
    assert "No module named" not in out
    assert out == "No syntax errors."
    assert any("pyflakes" in c for c in calls)
    assert any("py_compile" in c for c in calls)


def test_pyflakes_findings_are_still_returned(monkeypatch):
    """pyflakes exits non-zero when it finds real issues — that must not be
    mistaken for the tool being unavailable."""

    def fake_run(cmd, **kwargs):
        if "pyflakes" in cmd:
            return SimpleNamespace(
                returncode=1,
                stdout="/tmp/foo.py:1:1 'os' imported but unused\n",
                stderr="",
            )
        raise AssertionError("must not fall through when pyflakes works")

    monkeypatch.setattr(lsp.subprocess, "run", fake_run)
    out = lsp._python_diagnostics("/tmp/foo.py")
    assert "imported but unused" in out


# ── Fix (2): completed siblings keep real output on an approval pause ─────────


class _State:
    """Minimal stand-in for tool_executor.ToolState (the fields
    process_tool_results reads)."""

    def __init__(self, tool_id, tool_name, output, status="pending"):
        self.tool_id = tool_id
        self.tool_name = tool_name
        self.output = output
        self.side_effects = []
        self.status = status


def _budget_passthrough(_name, output):
    return output


def _patch_rules(monkeypatch, rules):
    monkeypatch.setattr(
        "server.security.permissions.load_permissions",
        lambda: {"mode": "default", "rules": rules},
    )


def test_completed_sibling_keeps_real_output_on_pause(monkeypatch):
    _patch_rules(monkeypatch, [])
    payload = '{"action": "ws_write_file", "path": "foo.txt", "content": "hi", "original": ""}'
    approval = _State("tu_write", "ws_write_file", f"[WS_APPROVAL]{payload}", status="completed")
    real_output = "print('hello world')  # already read"
    completed = _State("tu_read", "ws_read_file", real_output, status="completed")
    pending = _State("tu_pending", "ws_read_file", "", status="pending")

    tool_results, sse_events, has_pending_approval, has_user_question = asyncio.run(
        process_tool_results(
            [approval, completed, pending],
            budget_fn=_budget_passthrough,
            session_approvals={},
            config=None,
            model_id="",
            recent_messages=[],
            mode=MODE_DEFAULT,
        )
    )

    assert has_pending_approval is True
    by_id = {r["tool_use_id"]: r for r in tool_results}

    # The already-executed sibling keeps its real result (no "re-issue it").
    assert by_id["tu_read"]["content"] == real_output
    # The tool awaiting approval and the genuinely-unexecuted sibling are
    # labelled not-executed.
    assert by_id["tu_write"]["content"].startswith("[Not executed]")
    assert by_id["tu_pending"]["content"].startswith("[Not executed]")


# ── Fix (3): PTY fd close handoff ────────────────────────────────────────────


class _FakeProc:
    """Never dies on its own — so kill()/the _closing flag is what stops it."""

    def __init__(self, pid=424242):
        self.pid = pid

    def poll(self):
        return None


def _mk_session(master_fd):
    return T._PtySession(
        "sid", master_fd=master_fd, process=_FakeProc(), cols=80, rows=24, cwd="/tmp"
    )


def test_kill_defers_fd_close_to_reader(monkeypatch):
    sent, closed = [], []
    monkeypatch.setattr(T.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(T.os, "close", lambda fd: closed.append(fd))

    sess = _mk_session(4242)
    sess._reader_task = object()  # a reader is running and owns the close
    sess.kill()

    assert sess._closing is True
    assert sent == [(sess.process.pid, signal.SIGHUP)]
    # kill() must NOT close the fd out from under the reader's in-flight read.
    assert 4242 not in closed


def test_kill_closes_fd_when_no_reader(monkeypatch):
    sent, closed = [], []
    monkeypatch.setattr(T.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(T.os, "close", lambda fd: closed.append(fd))

    sess = _mk_session(7777)
    sess._reader_task = None  # no reader was ever started
    sess.kill()

    assert sess._closing is True
    # With no reader to own the close, kill() closes the fd itself.
    assert 7777 in closed


def test_reader_closes_fd_on_shutdown_flag():
    """End-to-end handoff: with the flag set, the reader exits and closes
    master_fd itself (kill never touches it)."""
    r, w = os.pipe()

    class _AliveProc:
        def poll(self):
            return None

    sess = T._PtySession("sid3", master_fd=r, process=_AliveProc(), cols=80, rows=24, cwd="/tmp")

    async def go():
        sess.output_event = asyncio.Event()
        task = asyncio.create_task(T._pty_reader(sess))
        await asyncio.sleep(0.05)  # let it enter the blocking-read loop
        sess._closing = True  # kill() sets this
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(go())

    # Reader owns the close and must have closed master_fd on exit.
    with pytest.raises(OSError):
        os.fstat(r)
    os.close(w)


# ── Fix (4): update_task rejects an invalid status ───────────────────────────


def _fresh(monkeypatch, tmp_path, session_id):
    monkeypatch.setattr(tt, "_DB_PATH", str(tmp_path / "tasks.db"))
    tt._task_store.pop(session_id, None)


def test_update_task_invalid_status_returns_none_no_bump(monkeypatch, tmp_path):
    sid = "sess-invalid-status"
    _fresh(monkeypatch, tmp_path, sid)
    task = tt.create_task(sid, "task X")
    before = task["updated_at"]

    result = tt.update_task(sid, task["id"], status="not-a-real-status")

    assert result is None
    stored = {t["id"]: t for t in tt._task_store[sid]}[task["id"]]
    assert stored["updated_at"] == before  # no false updated_at bump
    assert stored["status"] == "pending"  # status untouched


def test_update_task_valid_status_still_works(monkeypatch, tmp_path):
    sid = "sess-valid-status"
    _fresh(monkeypatch, tmp_path, sid)
    task = tt.create_task(sid, "task Y")

    result = tt.update_task(sid, task["id"], status="completed")

    assert result is not None
    assert result["status"] == "completed"


def test_execute_task_tool_reports_invalid_status(monkeypatch, tmp_path):
    sid = "sess-exec-invalid"
    _fresh(monkeypatch, tmp_path, sid)
    task = tt.create_task(sid, "task Z")

    out = json.loads(
        tt.execute_task_tool(
            "task_update",
            {"session_id": sid, "task_id": task["id"], "status": "bogus"},
        )
    )

    assert "error" in out
    assert out.get("updated") is None
    assert "bogus" in out["error"]
