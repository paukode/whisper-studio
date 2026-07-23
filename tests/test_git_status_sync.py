"""The /api/git/status endpoint reports ahead/behind vs upstream for the bar."""

import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.workspace.state as ws_state
from server.git.router import router


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo_pair(tmp_path):
    """A cloned repo so the working copy has a real upstream to diverge from."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-q", "--bare"], origin)

    work = tmp_path / "work"
    _git(["clone", "-q", str(origin), str(work)], tmp_path)
    _git(["config", "user.email", "t@t"], work)
    _git(["config", "user.name", "t"], work)
    (work / "a.txt").write_text("1")
    _git(["add", "."], work)
    _git(["commit", "-qm", "c1"], work)
    _git(["push", "-q", "origin", "HEAD:main"], work)
    _git(["branch", "--set-upstream-to=origin/main"], work)
    return work


@pytest.fixture
def client(repo_pair, monkeypatch):
    monkeypatch.setattr(ws_state, "load_workspace_config", lambda: {"path": str(repo_pair)})
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), repo_pair


def test_status_clean_reports_zero_ahead_behind(client):
    c, _work = client
    data = c.get("/api/git/status").json()
    assert data["clean"] is True
    assert data["ahead"] == 0
    assert data["behind"] == 0
    assert "branch" in data


def test_status_reports_ahead_after_local_commit(client):
    c, work = client
    (work / "b.txt").write_text("2")
    _git(["add", "."], work)
    _git(["commit", "-qm", "local"], work)
    data = c.get("/api/git/status").json()
    assert data["ahead"] == 1
    assert data["behind"] == 0


def test_status_reports_dirty_counts(client):
    c, work = client
    (work / "a.txt").write_text("changed")
    (work / "new.txt").write_text("untracked")
    data = c.get("/api/git/status").json()
    assert data["clean"] is False
    assert data["changed"] == 1
    assert data["untracked"] == 1


def test_status_no_upstream_degrades_to_zero(client, tmp_path, monkeypatch):
    """A repo with no upstream must still return status (ahead/behind 0),
    never fail the whole endpoint."""
    solo = tmp_path / "solo"
    solo.mkdir()
    _git(["init", "-q"], solo)
    _git(["config", "user.email", "t@t"], solo)
    _git(["config", "user.name", "t"], solo)
    (solo / "f").write_text("x")
    _git(["add", "."], solo)
    _git(["commit", "-qm", "c"], solo)
    monkeypatch.setattr(ws_state, "load_workspace_config", lambda: {"path": str(solo)})
    data = TestClient(_app_for()).get("/api/git/status").json()
    assert data["ahead"] == 0
    assert data["behind"] == 0


def _app_for():
    app = FastAPI()
    app.include_router(router)
    return app
