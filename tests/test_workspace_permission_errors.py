"""A folder macOS will not let the app read answers with the reason, never
as an empty folder (the tree, the folder picker, and /connect)."""

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.workspace.state as S
from server.workspace import router
from server.workspace.routes import browse


@pytest.fixture
def locked(tmp_path, monkeypatch):
    target = tmp_path / "locked"
    target.mkdir()
    real_listdir = os.listdir

    def listdir(p):
        if os.path.realpath(p) == os.path.realpath(str(target)):
            raise PermissionError(1, "Operation not permitted", str(target))
        return real_listdir(p)

    monkeypatch.setattr(os, "listdir", listdir)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), target


def test_the_folder_picker_reports_the_refusal(locked):
    c, target = locked
    r = c.get("/api/workspace/browse", params={"path": str(target)})
    assert r.status_code == 403
    assert "Operation not permitted" in r.json()["error"]


def test_the_tree_reports_the_refusal(locked, monkeypatch, tmp_path):
    c, target = locked
    monkeypatch.setattr(browse, "get_workspace_path", lambda: str(tmp_path))
    r = c.get("/api/workspace/list-dir", params={"path": "locked"})
    assert r.status_code == 403


def test_connect_refuses_a_folder_it_cannot_read(locked):
    c, target = locked
    r = c.post("/api/workspace/connect", json={"path": str(target)})
    assert r.status_code == 403
    assert "Cannot read" in r.json()["error"]
    assert S.get_workspace_path() is None
