"""The /api/git/file-diff endpoint feeds the side-by-side diff viewer."""

import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.workspace.state as ws_state
from server.git import file_diff
from server.git.router import router


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _git(["init", "-q"], work)
    _git(["config", "user.email", "t@t"], work)
    _git(["config", "user.name", "t"], work)
    (work / "a.txt").write_text("one\ntwo\nthree\n")
    (work / "bin.dat").write_bytes(b"\x00\x01\x02binary\x00")
    _git(["add", "."], work)
    _git(["commit", "-qm", "c1"], work)
    return work


@pytest.fixture
def client(repo, monkeypatch):
    monkeypatch.setattr(ws_state, "load_workspace_config", lambda: {"path": str(repo)})
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), repo


def test_modified_file_returns_both_sides_in_full_mode(client):
    c, work = client
    (work / "a.txt").write_text("one\nTWO\nthree\n")

    data = c.get("/api/git/file-diff", params={"path": "a.txt"}).json()

    assert data["mode"] == "full"
    assert data["status"] == "modified"
    assert data["old"] == "one\ntwo\nthree\n"
    assert data["new"] == "one\nTWO\nthree\n"
    assert data["added"] == 1
    assert data["removed"] == 1


def test_staged_changes_are_included(client):
    """Diffing against HEAD keeps the viewer consistent with the panel's
    +/- badges, which also come from `git diff HEAD`."""
    c, work = client
    (work / "a.txt").write_text("one\ntwo\nthree\nfour\n")
    _git(["add", "a.txt"], work)

    data = c.get("/api/git/file-diff", params={"path": "a.txt"}).json()

    assert data["mode"] == "full"
    assert data["added"] == 1
    assert data["new"].endswith("four\n")


def test_untracked_file_is_all_additions(client):
    c, work = client
    (work / "new.txt").write_text("alpha\nbeta\n")

    data = c.get("/api/git/file-diff", params={"path": "new.txt"}).json()

    assert data["mode"] == "full"
    assert data["status"] == "added"
    assert data["old"] == ""
    assert data["new"] == "alpha\nbeta\n"
    assert data["added"] == 2


def test_deleted_file_keeps_head_side(client):
    c, work = client
    (work / "a.txt").unlink()

    data = c.get("/api/git/file-diff", params={"path": "a.txt"}).json()

    assert data["status"] == "deleted"
    assert data["old"] == "one\ntwo\nthree\n"
    assert data["new"] == ""


def test_binary_file_reports_binary_mode(client):
    c, work = client
    (work / "bin.dat").write_bytes(b"\x00\x01\x02changed\x00")

    data = c.get("/api/git/file-diff", params={"path": "bin.dat"}).json()

    assert data["mode"] == "binary"


def test_untracked_binary_file_reports_binary_mode(client):
    c, work = client
    (work / "fresh.bin").write_bytes(b"\x00\xff\x00")

    data = c.get("/api/git/file-diff", params={"path": "fresh.bin"}).json()

    assert data["mode"] == "binary"


def test_missing_file_reports_error(client):
    c, _work = client

    data = c.get("/api/git/file-diff", params={"path": "nope.txt"}).json()

    assert data["mode"] == "error"
    assert data["error"]


def test_path_escaping_the_workspace_is_rejected(client):
    c, _work = client

    data = c.get("/api/git/file-diff", params={"path": "../outside.txt"}).json()

    assert data["mode"] == "error"
    assert data["error"] == "Invalid path."


def test_large_file_falls_back_to_hunks(client, monkeypatch):
    """Past the client's alignment budget git does the diff instead, and
    the payload carries hunks rather than whole blobs."""
    c, work = client
    monkeypatch.setattr(file_diff, "FULL_MODE_MAX_CELLS", 16)

    (work / "big.txt").write_text("".join(f"line {i}\n" for i in range(40)))
    _git(["add", "big.txt"], work)
    _git(["commit", "-qm", "big"], work)
    lines = [f"line {i}\n" for i in range(40)]
    lines[20] = "CHANGED\n"
    (work / "big.txt").write_text("".join(lines))

    data = c.get("/api/git/file-diff", params={"path": "big.txt"}).json()

    assert data["mode"] == "hunks"
    assert data["old"] is None and data["new"] is None
    assert data["added"] == 1
    assert data["removed"] == 1
    flat = [ln for h in data["hunks"] for ln in h["lines"]]
    changed = [ln for ln in flat if ln["type"] != "context"]
    assert {ln["text"] for ln in changed} == {"CHANGED", "line 20"}
    # Real line numbers on the side each line exists on.
    add = next(ln for ln in changed if ln["type"] == "add")
    assert add["new"] == 21 and add["old"] is None


def test_large_untracked_file_falls_back_to_capped_hunks(client, monkeypatch):
    c, work = client
    monkeypatch.setattr(file_diff, "FULL_MODE_MAX_CELLS", 16)
    monkeypatch.setattr(file_diff, "MAX_HUNK_LINES", 5)

    (work / "huge.txt").write_text("".join(f"row {i}\n" for i in range(20)))

    data = c.get("/api/git/file-diff", params={"path": "huge.txt"}).json()

    assert data["mode"] == "hunks"
    assert data["status"] == "added"
    assert data["truncated"] is True
    assert data["added"] == 20  # count is of the whole file, not the cap
    assert len(data["hunks"][0]["lines"]) == 5


def test_parse_unified_diff_tracks_both_line_numbers():
    raw = (
        "diff --git a/f b/f\n"
        "index 111..222 100644\n"
        "--- a/f\n"
        "+++ b/f\n"
        "@@ -10,4 +10,5 @@ def thing():\n"
        " keep one\n"
        "-drop me\n"
        "+add me\n"
        "+add two\n"
        " keep two\n"
        " keep three\n"
    )

    hunks, added, removed, truncated = file_diff.parse_unified_diff(raw)

    assert added == 2 and removed == 1 and truncated is False
    assert len(hunks) == 1
    hunk = hunks[0]
    assert hunk.old_start == 10 and hunk.new_start == 10
    assert hunk.header == "def thing():"
    assert [(ln.type, ln.old, ln.new) for ln in hunk.lines] == [
        ("context", 10, 10),
        ("del", 11, None),
        ("add", None, 11),
        ("add", None, 12),
        ("context", 12, 13),
        ("context", 13, 14),
    ]


def test_parse_unified_diff_ignores_no_newline_marker():
    raw = "@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file\n"

    hunks, added, removed, _ = file_diff.parse_unified_diff(raw)

    assert added == 1 and removed == 1
    assert [ln.text for ln in hunks[0].lines] == ["old", "new"]


def test_numstat_is_read_once_per_diff(client, monkeypatch):
    """Binary detection and the +/- counts both come from `git diff
    --numstat`. They used to be two separate invocations of the identical
    command, doubling the subprocess cost of every diff the viewer opens."""
    c, work = client
    (work / "a.txt").write_text("one\nTWO\nthree\n")

    numstat_calls = []
    real_run_git = file_diff._run_git

    def counting_run_git(args, **kwargs):
        if "--numstat" in args:
            numstat_calls.append(args)
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(file_diff, "_run_git", counting_run_git)

    data = c.get("/api/git/file-diff", params={"path": "a.txt"}).json()

    assert data["added"] == 1 and data["removed"] == 1
    assert len(numstat_calls) == 1, f"expected one numstat call, got {numstat_calls}"
