"""POST /api/workspace/mkdir makes a folder only inside ~/Documents,
~/Downloads, or the connected workspace. The check runs on the real path, so
a symlink or a ".." cannot carry the folder out of an allowed place."""

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import workspace
from server.workspace.routes import browse


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Downloads").mkdir()
    (home / "Desktop").mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _client(monkeypatch, ws: str | None) -> TestClient:
    monkeypatch.setattr(browse, "get_workspace_path", lambda: ws)
    app = FastAPI()
    app.include_router(workspace.router)
    return TestClient(app)


def _mkdir(client: TestClient, path: str):
    return client.post("/api/workspace/mkdir", json={"path": path})


@pytest.mark.parametrize("place", ["Documents", "Downloads"])
def test_allowed_in_documents_and_downloads_with_no_workspace(home, monkeypatch, place):
    client = _client(monkeypatch, None)
    target = home / place / "new" / "nested"

    r = _mkdir(client, str(target))

    assert r.status_code == 200
    assert r.json()["path"] == os.path.realpath(target)
    assert target.is_dir()


def test_tilde_path_is_allowed(home, monkeypatch):
    client = _client(monkeypatch, None)

    r = _mkdir(client, "~/Documents/project")

    assert r.status_code == 200
    assert (home / "Documents" / "project").is_dir()


def test_allowed_in_the_workspace_by_absolute_and_relative_path(home, tmp_path, monkeypatch):
    ws = tmp_path / "work"
    ws.mkdir()
    client = _client(monkeypatch, str(ws))

    assert _mkdir(client, str(ws / "a")).status_code == 200
    assert _mkdir(client, "b/c").status_code == 200
    assert (ws / "a").is_dir()
    assert (ws / "b" / "c").is_dir()


def test_refused_outside_the_allowed_places(home, tmp_path, monkeypatch):
    ws = tmp_path / "work"
    ws.mkdir()
    client = _client(monkeypatch, str(ws))

    for target in (home / "Desktop" / "x", home / "x", tmp_path / "elsewhere"):
        r = _mkdir(client, str(target))
        assert r.status_code == 403
        assert "~/Documents" in r.json()["error"]
        assert str(ws) in r.json()["error"]
        assert not target.exists()


def test_refused_through_a_symlink(home, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "Documents" / "link").symlink_to(outside)
    client = _client(monkeypatch, None)

    r = _mkdir(client, str(home / "Documents" / "link" / "escaped"))

    assert r.status_code == 403
    assert not (outside / "escaped").exists()


def test_refused_through_dot_dot(home, tmp_path, monkeypatch):
    ws = tmp_path / "work"
    ws.mkdir()
    client = _client(monkeypatch, str(ws))

    r = _mkdir(client, str(home / "Documents" / ".." / "Desktop" / "escaped"))

    assert r.status_code == 403
    assert not (home / "Desktop" / "escaped").exists()
    assert _mkdir(client, "../escaped").status_code == 403
    assert not (tmp_path / "escaped").exists()


def test_with_no_workspace_a_relative_path_and_other_folders_are_refused(
    home, tmp_path, monkeypatch
):
    client = _client(monkeypatch, None)

    r = _mkdir(client, "relative")
    assert r.status_code == 403
    assert "no workspace is connected" in r.json()["error"]
    assert _mkdir(client, str(tmp_path / "work" / "x")).status_code == 403
    assert not (tmp_path / "work").exists()


def test_an_existing_folder_is_a_conflict(home, monkeypatch):
    client = _client(monkeypatch, None)

    assert _mkdir(client, str(home / "Documents")).status_code == 409
