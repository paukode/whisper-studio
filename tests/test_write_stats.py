"""A write's diff stats are computed once, where the write happens.

_do_write (server/approval/executors.py, the executor behind write, edit and
create on every approval path) returns ApprovalOutcome.stats: the lines added
and removed against the file before the write, and the lines and bytes of
the file on disk after it, read back after ruff ran so a fix or a reformat
counts. The output text itself does not change: the claim reader and the
prompt depend on it. With a tool_use id on the payload the edit is kept for
the diff dialog (server/workspace/edits.py); without one nothing is stored.
A file that existed but could not be read leaves the change unknown, and a
failure in the stats never turns a write that happened into a failed one.
"""

import asyncio
import os

import pytest

from server import workspace
from server.approval.executors import _do_write
from server.workspace import edits

DIRTY = "import os\nimport sys\n\n\ndef f( x ):\n    return  x+1\n\n\nprint(sys.argv, f(1))\n"
RUFF_PYPROJECT = '[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = ["F"]\n'


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(workspace, "get_workspace_path", lambda: str(root))
    return root


def _write(payload: dict):
    outcome = asyncio.run(_do_write(payload))
    assert outcome.ok, outcome.error
    return outcome


def test_an_edit_counts_the_lines_it_added_and_removed(ws):
    (ws / "a.txt").write_text("one\ntwo\nthree\n")
    outcome = _write({"path": "a.txt", "content": "one\nTWO\nthree\nfour\n"})
    assert outcome.stats == {
        "added": 2,
        "removed": 1,
        "lines": 4,
        "bytes": len("one\nTWO\nthree\nfour\n"),
        "stored": False,
        "approximate": False,
    }
    # The text the model reads is the one it always read.
    assert outcome.output.startswith("Wrote a.txt (19 bytes). ")


def test_a_created_file_is_all_additions(ws):
    outcome = _write({"path": "new/b.txt", "content": "x\ny\nz\n"})
    assert outcome.stats["added"] == 3
    assert outcome.stats["removed"] == 0
    assert outcome.stats["lines"] == 3
    assert outcome.stats["bytes"] == 6


def test_the_stats_describe_the_file_after_ruff_reformatted_it(ws):
    (ws / "pyproject.toml").write_text(RUFF_PYPROJECT)
    outcome = _write({"path": "pkg/a.py", "content": DIRTY})
    on_disk = (ws / "pkg" / "a.py").read_text()
    assert on_disk != DIRTY  # ruff fixed and formatted the file
    assert outcome.stats["lines"] == len(on_disk.splitlines())
    assert outcome.stats["bytes"] == os.path.getsize(ws / "pkg" / "a.py")
    assert outcome.stats["added"] == len(on_disk.splitlines())
    assert outcome.stats["removed"] == 0
    # The byte count in the sentence is still the content as sent.
    assert outcome.output.startswith(f"Wrote pkg/a.py ({len(DIRTY)} bytes). ")
    assert "reformatted" in outcome.output


def test_the_edit_is_kept_under_the_calls_id_for_the_diff_dialog(ws):
    (ws / "a.txt").write_text("old\n")
    outcome = _write({"path": "a.txt", "content": "new\n", "__tool_use_id__": "toolu_01"})
    assert outcome.stats["stored"] is True
    record = edits.read_edit("toolu_01")
    assert record is not None
    assert record["path"] == "a.txt"
    assert record["original"] == "old\n"
    assert record["modified"] == "new\n"
    assert (record["added"], record["removed"]) == (1, 1)
    assert record["at"]


def test_a_created_files_record_has_an_empty_original(ws):
    _write({"path": "c.txt", "content": "fresh\n", "__tool_use_id__": "toolu_02"})
    record = edits.read_edit("toolu_02")
    assert record["original"] == ""
    assert record["modified"] == "fresh\n"


def test_a_line_ending_rewrite_counts_every_line_it_changed(ws):
    # The write stores LF, so a CRLF file changes on every line, as git
    # counts it; the stored original is the text that was on disk.
    (ws / "notes.txt").write_bytes(b"one\r\ntwo\r\nthree\r\nfour\r\n")
    outcome = _write(
        {"path": "notes.txt", "content": "one\nTWO\nthree\nfour\n", "__tool_use_id__": "toolu_eol"}
    )
    assert (ws / "notes.txt").read_bytes() == b"one\nTWO\nthree\nfour\n"
    assert (outcome.stats["added"], outcome.stats["removed"]) == (4, 4)
    record = edits.read_edit("toolu_eol")
    assert record["original"] == "one\r\ntwo\r\nthree\r\nfour\r\n"
    assert record["modified"] == "one\nTWO\nthree\nfour\n"
    # The undo backup keeps the text as an editor reads it.
    assert workspace.WORKSPACE_BACKUPS["notes.txt"] == "one\ntwo\nthree\nfour\n"


def test_the_record_tells_a_created_file_from_an_existing_empty_one(ws):
    (ws / "pkg").mkdir()
    (ws / "pkg" / "__init__.py").write_text("")
    _write({"path": "pkg/__init__.py", "content": "X = 1\n", "__tool_use_id__": "toolu_init"})
    _write({"path": "pkg/new.txt", "content": "X\n", "__tool_use_id__": "toolu_made"})
    assert edits.read_edit("toolu_init")["created"] is False
    assert edits.read_edit("toolu_made")["created"] is True


def test_a_write_with_no_call_id_stores_nothing(ws):
    outcome = _write({"path": "a.txt", "content": "x\n"})
    assert outcome.stats["stored"] is False
    assert not os.path.exists(edits.edits_dir()) or os.listdir(edits.edits_dir()) == []


def test_the_record_holds_the_file_as_ruff_left_it(ws):
    (ws / "pyproject.toml").write_text(RUFF_PYPROJECT)
    _write({"path": "m.py", "content": DIRTY, "__tool_use_id__": "toolu_03"})
    record = edits.read_edit("toolu_03")
    assert record["modified"] == (ws / "m.py").read_text()
    assert record["modified"] != DIRTY


def test_an_unreadable_file_is_not_counted_as_created(ws, monkeypatch):
    (ws / "a.txt").write_text("old\n")
    real_open = open

    def failing_first_read(file, *args, **kwargs):
        # The read before the write fails; the write and the read back work.
        if str(file).endswith("a.txt") and not args and "errors" in kwargs:
            monkeypatch.setattr("builtins.open", real_open)
            raise PermissionError("denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_first_read)
    outcome = _write({"path": "a.txt", "content": "new\nmore\n", "__tool_use_id__": "toolu_04"})
    assert (ws / "a.txt").read_text() == "new\nmore\n"
    assert outcome.stats["added"] is None and outcome.stats["removed"] is None
    assert outcome.stats["lines"] == 2
    assert outcome.stats["stored"] is False
    # No record: an empty original would make the overwrite look like a create.
    assert edits.read_edit("toolu_04") is None


def test_a_failure_in_the_stats_keeps_the_write_a_success(ws, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("stats broke")

    monkeypatch.setattr(edits, "diff_stats", boom)
    outcome = _write({"path": "a.txt", "content": "x\n"})
    assert outcome.ok is True
    assert outcome.stats is None
    assert outcome.output.startswith("Wrote a.txt (2 bytes). ")
    assert (ws / "a.txt").read_text() == "x\n"
