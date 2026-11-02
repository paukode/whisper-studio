"""Undo of one approved write (server/workspace/write_undo.py and
POST /api/workspace/undo).

The undo copy is taken when the approved write runs, keyed by its tool_use
id, so Undo puts back the file from immediately before THAT write. It
refuses, and changes nothing, when the file is not as the write left it.
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import workspace
from server.approval.executors import _do_write
from server.workspace import router as ws_router
from server.workspace import write_undo
from server.workspace.routes import file_ops


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(workspace, "get_workspace_path", lambda: str(root))
    monkeypatch.setattr(file_ops, "get_workspace_path", lambda: str(root))
    write_undo.clear_write_backups()
    yield root
    write_undo.clear_write_backups()


def _client():
    app = FastAPI()
    app.include_router(ws_router)
    return TestClient(app)


def _write(path: str, content: str, tool_use_id: str | None):
    payload = {"path": path, "content": content}
    if tool_use_id:
        payload["__tool_use_id__"] = tool_use_id
    outcome = asyncio.run(_do_write(payload))
    assert outcome.ok, outcome.error


def _undo(tool_use_id: str):
    return _client().post("/api/workspace/undo", json={"tool_use_id": tool_use_id})


def test_undo_after_one_write_restores_the_exact_bytes(ws):
    (ws / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    _write("a.txt", "ONE\nTWO\n", "toolu_1")
    assert (ws / "a.txt").read_bytes() == b"ONE\nTWO\n"
    resp = _undo("toolu_1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["restored"] is True
    assert (ws / "a.txt").read_bytes() == b"one\r\ntwo\r\n"
    # The copy is used up: a second undo has nothing to put back.
    assert _undo("toolu_1").status_code == 404


def test_two_writes_to_one_file_undo_in_order(ws):
    (ws / "a.txt").write_text("v0\n")
    _write("a.txt", "v1\n", "toolu_a")
    _write("a.txt", "v2\n", "toolu_b")
    # The earlier write first: the file is not as that write left it.
    early = _undo("toolu_a")
    assert early.status_code == 409
    assert (ws / "a.txt").read_text() == "v2\n"
    # The later write puts back the first write's text, then the first
    # write puts back the original.
    assert _undo("toolu_b").status_code == 200
    assert (ws / "a.txt").read_text() == "v1\n"
    assert _undo("toolu_a").status_code == 200
    assert (ws / "a.txt").read_text() == "v0\n"


def test_undo_is_refused_after_an_outside_edit(ws):
    (ws / "a.txt").write_text("before\n")
    _write("a.txt", "after\n", "toolu_1")
    (ws / "a.txt").write_text("after\nthe user typed this\n")
    resp = _undo("toolu_1")
    assert resp.status_code == 409
    assert "changed after" in resp.json()["error"]
    assert (ws / "a.txt").read_text() == "after\nthe user typed this\n"
    # The copy is still there for when the user puts the file back.
    assert write_undo.has_undo("toolu_1")


def test_undo_of_a_created_file_removes_it(ws):
    _write("new.txt", "fresh\n", "toolu_new")
    resp = _undo("toolu_new")
    assert resp.status_code == 200
    assert resp.json()["removed"] is True
    assert not (ws / "new.txt").exists()


def test_undo_is_refused_when_the_file_is_gone(ws):
    (ws / "a.txt").write_text("before\n")
    _write("a.txt", "after\n", "toolu_1")
    (ws / "a.txt").unlink()
    assert _undo("toolu_1").status_code == 409
    assert not (ws / "a.txt").exists()


def test_a_pending_or_unknown_write_has_no_undo(ws):
    # A card that was never approved ran no write: there is no copy, the
    # edit route says it is not undoable, and the undo route refuses.
    (ws / "a.txt").write_text("keep\n")
    assert not write_undo.has_undo("toolu_pending")
    assert _undo("toolu_pending").status_code == 404
    assert (ws / "a.txt").read_text() == "keep\n"


def test_a_write_without_an_id_keeps_no_copy(ws):
    (ws / "a.txt").write_text("before\n")
    _write("a.txt", "after\n", None)
    assert write_undo._BACKUPS == {}


def test_the_edit_record_says_whether_undo_is_offered(ws):
    (ws / "a.txt").write_text("before\n")
    _write("a.txt", "after\n", "toolu_rec")
    client = _client()
    assert client.get("/api/workspace/edits/toolu_rec").json()["undoable"] is True
    assert _undo("toolu_rec").status_code == 200
    assert client.get("/api/workspace/edits/toolu_rec").json()["undoable"] is False


def test_undo_needs_a_tool_use_id(ws):
    resp = _client().post("/api/workspace/undo", json={"path": "a.txt"})
    assert resp.status_code == 400


def test_the_oldest_copies_go_first_over_the_cap(ws, monkeypatch):
    monkeypatch.setattr(write_undo, "UNDO_TOTAL_CAP", 10)
    (ws / "a.txt").write_text("123456\n")
    (ws / "b.txt").write_text("abcdef\n")
    _write("a.txt", "x\n", "toolu_a")
    _write("b.txt", "y\n", "toolu_b")
    assert not write_undo.has_undo("toolu_a")
    assert write_undo.has_undo("toolu_b")


def test_a_workspace_change_clears_the_copies(ws, tmp_path, monkeypatch):
    from server.workspace import state

    (ws / "a.txt").write_text("before\n")
    _write("a.txt", "after\n", "toolu_1")
    monkeypatch.setattr(state, "WORKSPACE_CONFIG_PATH", str(tmp_path / "workspace.json"))
    monkeypatch.setattr(state, "RECENT_WORKSPACES_PATH", str(tmp_path / "recent.json"))
    monkeypatch.setattr(state, "_point_git_watcher_at", lambda _p: None)
    monkeypatch.setattr(state, "_point_workspace_watcher_at", lambda _p: None)
    other = tmp_path / "other"
    other.mkdir()
    state.connect_workspace(str(other), by_user=True)
    assert not write_undo.has_undo("toolu_1")
