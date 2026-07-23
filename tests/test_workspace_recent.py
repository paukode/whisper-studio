"""Recent-workspace removal: indexed folders are protected; unindexed ones can
be removed one at a time or cleared in bulk."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.index.store as store
import server.workspace.routes.recent as wrecent
import server.workspace.state as state
from server.workspace import router as ws_router


def _client(monkeypatch, tmp_path, recents, indexed):
    recent_file = tmp_path / "recent_workspaces.json"
    recent_file.write_text(json.dumps(recents))
    monkeypatch.setattr(state, "RECENT_WORKSPACES_PATH", str(recent_file))
    monkeypatch.setattr(wrecent, "RECENT_WORKSPACES_PATH", str(recent_file))
    monkeypatch.setattr(store, "list_indexed_workspaces", lambda: indexed)
    app = FastAPI()
    app.include_router(ws_router)
    return TestClient(app)


def test_recent_remove_protects_indexed(monkeypatch, tmp_path):
    c = _client(
        monkeypatch,
        tmp_path,
        recents=["/ws/indexed", "/ws/temp", "/ws/notes"],
        indexed=["/ws/indexed"],
    )
    # Indexed folder: removal is a no-op (stays in the list).
    r = c.post("/api/workspace/recent/remove", json={"path": "/ws/indexed"})
    assert "/ws/indexed" in r.json()["recent"]
    # Unindexed folder: removed.
    r = c.post("/api/workspace/recent/remove", json={"path": "/ws/temp"})
    recent = r.json()["recent"]
    assert "/ws/temp" not in recent and "/ws/indexed" in recent and "/ws/notes" in recent


def test_recent_clear_unindexed_keeps_only_indexed(monkeypatch, tmp_path):
    c = _client(
        monkeypatch,
        tmp_path,
        recents=["/ws/indexed", "/ws/temp", "/ws/notes"],
        indexed=["/ws/indexed"],
    )
    r = c.post("/api/workspace/recent/clear-unindexed", json={})
    assert r.json()["recent"] == ["/ws/indexed"]
