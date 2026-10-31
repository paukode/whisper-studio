"""/open-with and /reveal must also accept an absolute path inside a
directory the one-shot save_file flow wrote to (server/workspace/state.py's
record_save_location / load_save_locations) — even though that file was
never 'connected' as a workspace nor 'indexed'. Mirrors the existing
indexed-workspace acceptance pinned in test_created_file_link.py.
"""

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import workspace
from server.workspace.routes import os_integration


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(workspace.router)
    return TestClient(app)


def _isolated(tmp_path, monkeypatch):
    """No connected workspace, no indexed workspaces — only the
    save-locations list can grant access."""
    monkeypatch.setattr(os_integration, "get_workspace_path", lambda: None)
    monkeypatch.setattr("server.index.store.list_indexed_workspaces", lambda: [])


def test_open_with_accepts_a_save_file_destination(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    saved_dir = tmp_path / "saved-here"
    saved_dir.mkdir()
    f = saved_dir / "report.pdf"
    f.write_text("hi")
    monkeypatch.setattr(os_integration, "load_save_locations", lambda: [str(saved_dir)])

    with patch("server.workspace.routes.os_integration.subprocess.Popen") as popen:
        resp = _client().post("/api/workspace/open-with", json={"path": str(f)})

    assert resp.status_code == 200
    assert resp.json() == {"path": str(f), "opened": True}
    popen.assert_called_once_with(["open", str(f)])


def test_reveal_accepts_a_save_file_destination(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    saved_dir = tmp_path / "saved-here"
    saved_dir.mkdir()
    f = saved_dir / "export.csv"
    f.write_text("a,b,c")
    monkeypatch.setattr(os_integration, "load_save_locations", lambda: [str(saved_dir)])

    with patch("server.workspace.routes.os_integration.subprocess.Popen") as popen:
        resp = _client().post("/api/workspace/reveal", json={"path": str(f)})

    assert resp.status_code == 200
    assert resp.json() == {"path": str(f), "revealed": True}
    popen.assert_called_once_with(["open", "-R", str(f)])


def test_open_with_still_rejects_paths_outside_every_root(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    saved_dir = tmp_path / "saved-here"
    saved_dir.mkdir()
    monkeypatch.setattr(os_integration, "load_save_locations", lambda: [str(saved_dir)])

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    f = outside / "secret.md"
    f.write_text("hi")

    resp = _client().post("/api/workspace/open-with", json={"path": str(f)})
    assert resp.status_code == 404


def test_save_locations_source_failure_is_non_fatal(tmp_path, monkeypatch):
    # If load_save_locations() ever raised, it must not break /open-with for
    # files reachable through the workspace/indexed roots.
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "in-workspace.txt"
    f.write_text("hi")
    monkeypatch.setattr(os_integration, "get_workspace_path", lambda: str(ws))
    monkeypatch.setattr("server.index.store.list_indexed_workspaces", lambda: [])

    def _boom():
        raise RuntimeError("disk error")

    monkeypatch.setattr(os_integration, "load_save_locations", _boom)

    with patch("server.workspace.routes.os_integration.subprocess.Popen") as popen:
        resp = _client().post("/api/workspace/open-with", json={"path": str(f)})

    assert resp.status_code == 200
    popen.assert_called_once_with(["open", str(f)])
