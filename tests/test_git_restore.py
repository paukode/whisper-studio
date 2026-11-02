"""POST /api/git/restore undoes one file and never reaches outside the
workspace or the workspace root itself."""

import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.workspace.state as ws_state
from server.git.router import router


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def client(tmp_path, monkeypatch):
    work = tmp_path / "repo"
    work.mkdir()
    _git(["init", "-q"], work)
    _git(["config", "user.email", "t@t"], work)
    _git(["config", "user.name", "t"], work)
    (work / "a.txt").write_text("one\n")
    _git(["add", "."], work)
    _git(["commit", "-qm", "c1"], work)
    monkeypatch.setattr(ws_state, "load_workspace_config", lambda: {"path": str(work)})
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), work


@pytest.mark.parametrize("path", ["", ".", "./", "sub/.."])
def test_the_workspace_root_is_refused(client, path):
    c, work = client
    res = c.post("/api/git/restore", json={"path": path})
    assert res.status_code == 400
    assert (work / ".git").is_dir()
    assert (work / "a.txt").exists()


def test_a_sibling_with_the_same_name_prefix_is_refused(client):
    c, work = client
    sibling = work.parent / f"{work.name}2"
    sibling.mkdir()
    (sibling / "important.txt").write_text("keep\n")

    res = c.post("/api/git/restore", json={"path": f"../{work.name}2/important.txt"})

    assert res.status_code == 400
    assert (sibling / "important.txt").read_text() == "keep\n"


def test_a_symlinked_folder_that_leads_out_is_refused(client):
    c, work = client
    outside = work.parent / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("keep\n")
    (work / "link").symlink_to(outside)

    res = c.post("/api/git/restore", json={"path": "link/f.txt"})

    assert res.status_code == 400
    assert (outside / "f.txt").exists()


def test_an_untracked_folder_is_removed(client):
    c, work = client
    (work / "new").mkdir()
    (work / "new" / "x.txt").write_text("x\n")

    res = c.post("/api/git/restore", json={"path": "new"})

    assert res.status_code == 200
    assert not (work / "new").exists()


def test_a_tracked_file_is_restored_from_head(client):
    c, work = client
    (work / "a.txt").write_text("changed\n")

    res = c.post("/api/git/restore", json={"path": "a.txt"})

    assert res.status_code == 200
    assert (work / "a.txt").read_text() == "one\n"
