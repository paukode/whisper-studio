"""Recent-workspace removal: indexed folders are protected; unindexed ones can
be removed one at a time or cleared in bulk."""

import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.index.store as store
import server.workspace.state as state
from server.workspace import router as ws_router


def _client(monkeypatch, tmp_path, recents, indexed):
    recent_file = tmp_path / "recent_workspaces.json"
    recent_file.write_text(json.dumps(recents))
    monkeypatch.setattr(state, "RECENT_WORKSPACES_PATH", str(recent_file))
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


def test_an_index_store_error_fails_and_keeps_every_recent(monkeypatch, tmp_path):
    """A store failure used to read as 'nothing is indexed' and wipe the list."""
    c = _client(monkeypatch, tmp_path, recents=["/ws/indexed", "/ws/temp"], indexed=[])

    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "list_indexed_workspaces", broken)
    c = TestClient(c.app, raise_server_exceptions=False)
    r = c.post("/api/workspace/recent/clear-unindexed")
    assert r.status_code == 500
    assert json.loads((tmp_path / "recent_workspaces.json").read_text()) == [
        "/ws/indexed",
        "/ws/temp",
    ]


def test_a_damaged_recents_file_is_an_error_not_an_empty_list(monkeypatch, tmp_path):
    recent_file = tmp_path / "recent_workspaces.json"
    recent_file.write_text('["/ws/a",')
    monkeypatch.setattr(state, "RECENT_WORKSPACES_PATH", str(recent_file))
    with pytest.raises(ValueError, match="recent_workspaces.json"):
        state.load_recent_workspaces()
    with pytest.raises(ValueError):
        state.save_recent_workspace("/ws/b")
    assert recent_file.read_text() == '["/ws/a",'


def test_a_damaged_workspace_config_is_an_error_not_a_disconnect(monkeypatch, tmp_path):
    cfg = tmp_path / "workspace_config.json"
    cfg.write_text('{"path": ')
    monkeypatch.setattr(state, "WORKSPACE_CONFIG_PATH", str(cfg))
    with pytest.raises(ValueError, match="workspace_config.json"):
        state.load_workspace_config()
    cfg.write_text("[]")
    with pytest.raises(ValueError, match="expected a JSON dict"):
        state.load_workspace_config()


def _damaged_config(monkeypatch, tmp_path):
    cfg = tmp_path / "workspace_config.json"
    cfg.write_text('{"path": ')
    monkeypatch.setattr(state, "WORKSPACE_CONFIG_PATH", str(cfg))
    monkeypatch.setattr(state, "RECENT_WORKSPACES_PATH", str(tmp_path / "recent.json"))
    monkeypatch.setattr(state, "_point_git_watcher_at", lambda _p: None)
    monkeypatch.setattr(state, "_point_workspace_watcher_at", lambda _p: None)
    return cfg


def test_the_users_connect_replaces_a_damaged_workspace_config(monkeypatch, tmp_path):
    cfg = _damaged_config(monkeypatch, tmp_path)
    folder = tmp_path / "proj"
    folder.mkdir()
    epoch = state._release_epoch
    app = FastAPI()
    app.include_router(ws_router)

    r = TestClient(app).post("/api/workspace/connect", json={"path": str(folder)})

    assert r.status_code == 200
    (warning,) = r.json()["warnings"]
    assert "damaged workspace config was replaced" in warning
    assert json.loads(cfg.read_text()) == {"path": os.path.realpath(folder)}
    # The workspace it held is unknown: a running turn is released from it.
    assert state._release_epoch == epoch + 1


def test_a_tools_connect_still_refuses_a_damaged_workspace_config(monkeypatch, tmp_path):
    cfg = _damaged_config(monkeypatch, tmp_path)
    folder = tmp_path / "proj"
    folder.mkdir()

    with pytest.raises(ValueError, match="workspace_config.json"):
        state.connect_workspace(str(folder))
    assert cfg.read_text() == '{"path": '


def test_the_users_disconnect_replaces_a_damaged_workspace_config(monkeypatch, tmp_path):
    cfg = _damaged_config(monkeypatch, tmp_path)
    app = FastAPI()
    app.include_router(ws_router)

    r = TestClient(app).post("/api/workspace/disconnect")

    assert r.status_code == 200
    assert any("damaged workspace config was replaced" in w for w in r.json()["warnings"])
    assert json.loads(cfg.read_text()) == {"path": None}
