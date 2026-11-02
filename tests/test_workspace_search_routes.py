"""The context-menu search and file history routes: one ripgrep path, no
flag injection through the pattern, and git errors reported as errors."""

import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.workspace import router
from server.workspace.routes import search


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(search, "get_workspace_path", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False), tmp_path


def test_a_pattern_that_looks_like_a_flag_is_searched_not_obeyed(client):
    c, ws = client
    marker = ws / "ran"
    (ws / "a.txt").write_text(f"--pre={marker}\n")

    r = c.get("/api/workspace/grep", params={"pattern": f"--pre={marker}"})

    assert r.status_code == 200
    assert [m["path"] for m in r.json()["results"]] == ["a.txt"]
    assert not marker.exists()


def test_rg_missing_is_an_error_not_a_grep_run(client, monkeypatch):
    c, ws = client
    (ws / "a.txt").write_text("hello\n")

    def missing():
        raise RuntimeError("ripgrep (rg) not found")

    monkeypatch.setattr("server.search.engine.get_rg_path", missing)
    r = c.get("/api/workspace/grep", params={"pattern": "hello"})
    assert r.status_code == 500
    # The JSON {error} shape the UI reads, with the resolver's reason.
    assert "ripgrep (rg) not found" in r.json()["error"]


def test_file_history_outside_a_repository_is_an_error(client):
    c, ws = client
    (ws / "a.txt").write_text("x\n")

    r = c.get("/api/workspace/file-history", params={"path": "a.txt"})

    assert r.status_code == 500
    assert "not a git repository" in r.json()["error"]


def test_file_history_lists_commits(client):
    c, ws = client
    run = lambda *a: subprocess.run(["git", *a], cwd=ws, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (ws / "a.txt").write_text("x\n")
    run("add", "a.txt")
    run("commit", "-qm", "first")

    r = c.get("/api/workspace/file-history", params={"path": "a.txt", "limit": "5"})

    assert r.status_code == 200
    assert [h["message"] for h in r.json()["history"]] == ["first"]
    assert (
        c.get("/api/workspace/file-history", params={"path": "a.txt", "limit": "x"}).status_code
        == 422
    )
