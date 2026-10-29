"""The write gate after the app's own writes (server/file_state.py).

A field session lost about two dozen edits to "File modified since last
read" right after its own successful edit of the same file, and to "Must
read file before writing" right after it created the file: the approved
write never told the read record. These pin that a session's own write,
edit or create keeps the file writable, that a change from outside still
asks for a re-read, and that a relative and an absolute path are one file.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from server import file_state
from server.approval.executors import _do_write
from server.workspace import executors

SID = "own-writes"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(executors, "get_workspace_path", lambda: str(tmp_path))
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: str(tmp_path))
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(tmp_path))
    yield tmp_path
    file_state.clear_session(SID)


def _call(executor, **given) -> str:
    return executor({**given, "__session_id__": SID}, [], [])


def _approve(out: str) -> str:
    """Run an approval request the way the gate runs it inline in Bypass."""
    assert out.startswith("[WS_APPROVAL]"), out
    outcome = asyncio.run(_do_write(json.loads(out[len("[WS_APPROVAL]") :])))
    assert outcome.ok, outcome.error
    return outcome.output


def _edit(path: str, old: str, new: str) -> str:
    return _call(executors._exec_ws_edit_file, path=path, old_string=old, new_string=new)


def test_a_second_edit_after_the_sessions_own_edit_needs_no_reread(ws):
    (ws / "a.txt").write_text("one\ntwo\n")
    _call(executors._exec_ws_read_file, path="a.txt")
    _approve(_edit("a.txt", "one", "uno"))
    _approve(_edit("a.txt", "two", "dos"))
    assert (ws / "a.txt").read_text() == "uno\ndos\n"


def test_a_file_the_session_created_can_be_edited_at_once(ws):
    _approve(_call(executors._exec_ws_create_file, path="new.txt", content="alpha\n"))
    _approve(_edit("new.txt", "alpha", "beta"))
    assert (ws / "new.txt").read_text() == "beta\n"


def test_a_change_from_outside_still_asks_for_a_reread(ws):
    f = ws / "a.txt"
    f.write_text("one\n")
    _call(executors._exec_ws_read_file, path="a.txt")
    _approve(_edit("a.txt", "one", "uno"))
    f.write_text("changed elsewhere\n")
    later = os.path.getmtime(f) + 5
    os.utime(f, (later, later))
    assert _edit("a.txt", "changed", "x") == (
        "Error: File modified since last read. Re-read before writing."
    )


def test_a_relative_read_and_an_absolute_edit_are_one_file(ws):
    (ws / "a.txt").write_text("one\n")
    _call(executors._exec_ws_read_file, path="a.txt")
    out = _edit(str(ws / "a.txt"), "one", "uno")
    assert out.startswith("[WS_APPROVAL]"), out


def test_a_read_after_a_write_returns_the_content_not_the_stub(ws):
    (ws / "a.txt").write_text("one\n")
    _call(executors._exec_ws_read_file, path="a.txt")
    _approve(_edit("a.txt", "one", "uno"))
    assert "uno" in _call(executors._exec_ws_read_file, path="a.txt")


def test_another_session_still_has_to_read_first(ws):
    (ws / "a.txt").write_text("one\n")
    _call(executors._exec_ws_read_file, path="a.txt")
    _approve(_edit("a.txt", "one", "uno"))
    other = executors._exec_ws_edit_file(
        {"path": "a.txt", "old_string": "uno", "new_string": "x", "__session_id__": "other"},
        [],
        [],
    )
    assert other == "Error: Must read file before writing."
