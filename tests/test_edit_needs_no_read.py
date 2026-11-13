"""An edit needs no read first; a whole-file write still does.

In field sessions the most frequent failed tool calls were ws_edit_file
refused with "Only partial read recorded" and "Must read file before
writing", and a live turn lost two calls on each edit because the model read
the file with ``cat`` in a shell command. An edit replaces text that must
match the file on disk exactly, so it cannot act on content the model did
not see: it has no read gate. ws_write_file replaces the whole file, so it
keeps the gate, and an edit of a file the session never read whole does not
open that gate.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from server import file_state
from server.approval.executors import _do_write
from server.workspace import executors

SID = "edit-no-read"
BODY = "".join(f"line {n}\n" for n in range(1, 41))


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(executors, "get_workspace_path", lambda: str(tmp_path))
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: str(tmp_path))
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(tmp_path))
    (tmp_path / "big.txt").write_text(BODY)
    yield tmp_path
    file_state.clear_session(SID)


def _call(executor, **given) -> str:
    return executor({**given, "__session_id__": SID}, [], [])


def _approve(out: str) -> str:
    assert out.startswith("[WS_APPROVAL]"), out
    outcome = asyncio.run(_do_write(json.loads(out[len("[WS_APPROVAL]") :])))
    assert outcome.ok, outcome.error
    return outcome.output


def _read(**given) -> str:
    return _call(executors._exec_ws_read_file, path="big.txt", **given)


def _edit(old: str, new: str) -> str:
    return _call(executors._exec_ws_edit_file, path="big.txt", old_string=old, new_string=new)


def _write(content: str = "new\n") -> str:
    return _call(executors._exec_ws_write_file, path="big.txt", content=content)


def test_an_edit_of_a_file_never_read_is_not_refused(ws):
    _approve(_edit("line 12\n", "twelve\n"))
    assert "twelve\n" in (ws / "big.txt").read_text()


def test_an_edit_after_a_read_of_one_part_is_not_refused(ws):
    _read(offset=10, limit=5)
    _approve(_edit("line 30\n", "thirty\n"))


def test_an_edit_after_a_change_on_disk_matches_the_file_as_it_is(ws):
    _read()
    f = ws / "big.txt"
    f.write_text(BODY.replace("line 12\n", "changed elsewhere\n"))
    later = os.path.getmtime(f) + 5
    os.utime(f, (later, later))
    assert _edit("line 12\n", "x\n").startswith("Error: old_string not found")
    _approve(_edit("changed elsewhere\n", "x\n"))


def test_a_whole_file_write_needs_a_read(ws):
    assert _write().startswith("Error: Read the file first")


def test_a_whole_file_write_needs_the_whole_file_read(ws):
    _read(offset=10, limit=5)
    assert _write().startswith("Error: Only part of the file was read.")


def test_a_read_with_a_limit_past_the_end_counts_as_the_whole_file(ws):
    _read(offset=1, limit=500)
    assert _write().startswith("[WS_APPROVAL]")


def test_a_part_read_after_a_whole_read_keeps_the_whole_read(ws):
    _read()
    _read(offset=5, limit=2)
    assert _write().startswith("[WS_APPROVAL]")


def test_an_edit_of_a_file_never_read_does_not_open_the_whole_file_write(ws):
    _approve(_edit("line 12\n", "twelve\n"))
    assert _write().startswith("Error: Only part of the file was read.")


def test_an_edit_of_a_file_read_whole_keeps_the_whole_file_write_open(ws):
    _read()
    _approve(_edit("line 12\n", "twelve\n"))
    assert _write().startswith("[WS_APPROVAL]")
