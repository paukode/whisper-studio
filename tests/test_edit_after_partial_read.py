"""An edit needs only the lines it replaces to have been read.

The most frequent failed tool call in field sessions was ws_edit_file refused
with "Only partial read recorded" after the model read one part of a large
file and edited inside that part, or read the whole file and then looked at
one part again. server/file_state.py keeps the line ranges each read
returned: an edit passes when its text sits in lines the session read, a
whole-file write still needs a read of the whole file, and the refusal names
the lines to read.
"""

from __future__ import annotations

import pytest

from server import file_state
from server.workspace import executors

SID = "partial-read"
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


def _read(**given) -> str:
    return _call(executors._exec_ws_read_file, path="big.txt", **given)


def _edit(old: str, new: str, **given) -> str:
    return _call(
        executors._exec_ws_edit_file, path="big.txt", old_string=old, new_string=new, **given
    )


def test_an_edit_inside_the_lines_read_is_not_refused(ws):
    _read(offset=10, limit=5)
    assert _edit("line 12\n", "twelve\n").startswith("[WS_APPROVAL]")


def test_an_edit_outside_the_lines_read_names_the_lines_to_read(ws):
    _read(offset=10, limit=5)
    out = _edit("line 30\nline 31\n", "x\n")
    assert out.startswith("Error: The text to replace is at lines 30 to 31")
    assert "offset 30" in out
    _read(offset=30, limit=2)
    assert _edit("line 30\nline 31\n", "x\n").startswith("[WS_APPROVAL]")


def test_reads_of_two_parts_cover_an_edit_across_both(ws):
    _read(offset=10, limit=5)
    _read(offset=15, limit=5)
    assert _edit("line 14\nline 15\n", "x\n").startswith("[WS_APPROVAL]")


def test_a_part_read_after_a_whole_read_keeps_the_whole_read(ws):
    _read()
    _read(offset=5, limit=2)
    assert _edit("line 33\n", "x\n").startswith("[WS_APPROVAL]")
    out = _call(executors._exec_ws_write_file, path="big.txt", content="new\n")
    assert out.startswith("[WS_APPROVAL]")


def test_replace_all_needs_every_match_read(ws):
    (ws / "big.txt").write_text("same\n" + BODY + "same\n")
    _read(offset=1, limit=3)
    assert _edit("same\n", "x\n", replace_all=True).startswith(
        "Error: The text to replace is at line 42"
    )


def test_a_whole_file_write_still_needs_the_whole_file_read(ws):
    _read(offset=10, limit=5)
    out = _call(executors._exec_ws_write_file, path="big.txt", content="new\n")
    assert out.startswith("Error: Only part of the file was read.")


def test_a_read_with_a_limit_past_the_end_counts_as_the_whole_file(ws):
    _read(offset=1, limit=500)
    out = _call(executors._exec_ws_write_file, path="big.txt", content="new\n")
    assert out.startswith("[WS_APPROVAL]")


def test_a_change_on_disk_drops_the_ranges(ws):
    import os

    _read(offset=10, limit=5)
    f = ws / "big.txt"
    later = os.path.getmtime(f) + 5
    os.utime(f, (later, later))
    assert _edit("line 12\n", "x\n").startswith("Error: The file changed on disk")
    _read(offset=20, limit=2)
    assert _edit("line 12\n", "x\n").startswith("Error: The text to replace is at line 12")
