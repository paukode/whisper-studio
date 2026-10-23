"""`git status --porcelain` parsing keeps the index column of every entry.

Stripping the whole output removed the leading space of the first entry, so
" M first.txt" (changed in the worktree only) read as "M " and the Git panel
listed it under Staged.
"""

import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.git.core as git_core
import server.git.router as git_router
import server.workspace.state as ws_state
from server.git.core import STATUS_PORCELAIN_ARGS, parse_porcelain_status

UNSTAGED_FIRST = " M first.txt\0 M second.txt\0M  staged.txt\0"


def _fake_git(stdout: str):
    """Answer the status call with `stdout`; any other git call fails."""

    def run(args, cwd, **_kwargs):
        if args == STATUS_PORCELAIN_ARGS:
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="no upstream")

    return run


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A Git router on a workspace at tmp_path/repo (the caller adds .git)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(ws_state, "load_workspace_config", lambda: {"path": str(repo)})
    git_router.invalidate_changes_cache()
    app = FastAPI()
    app.include_router(git_router.router)
    yield TestClient(app), repo
    git_router.invalidate_changes_cache()


def _staged_by_path(files):
    return {f["path"]: f["staged"] for f in files}


def test_first_entry_keeps_its_blank_index_column():
    entries = parse_porcelain_status(UNSTAGED_FIRST)
    assert entries == [(" M", "first.txt"), (" M", "second.txt"), ("M ", "staged.txt")]


def test_paths_with_spaces_are_unquoted():
    entries = parse_porcelain_status(" M sp ace.txt\0?? new dir/\0")
    assert entries == [(" M", "sp ace.txt"), ("??", "new dir/")]


def test_rename_reports_the_new_path_and_skips_the_source():
    entries = parse_porcelain_status("R  new.txt\0old.txt\0 M after.txt\0")
    assert entries == [("R ", "new.txt"), (" M", "after.txt")]


def test_empty_output_has_no_entries():
    assert parse_porcelain_status("") == []


def test_get_file_status_reads_the_first_unstaged_entry(monkeypatch):
    monkeypatch.setattr(git_core, "_run_git", _fake_git(" M first.txt\0?? sp ace.txt\0"))
    assert git_core.get_file_status("/repo") == {
        "tracked": ["first.txt"],
        "untracked": ["sp ace.txt"],
    }


def test_changes_lists_the_first_unstaged_entry_as_unstaged(client, monkeypatch):
    c, repo = client
    (repo / ".git").mkdir()
    monkeypatch.setattr(git_router, "_run_git", _fake_git(UNSTAGED_FIRST))
    monkeypatch.setattr(git_router, "fetch_git_diff", lambda _ws: None)
    monkeypatch.setattr(git_router, "get_branch", lambda _ws: "main")
    files = c.get("/api/git/changes").json()["files"]
    assert _staged_by_path(files) == {
        "first.txt": False,
        "second.txt": False,
        "staged.txt": True,
    }


def test_status_counts_the_first_unstaged_entry(client, monkeypatch):
    c, repo = client
    (repo / ".git").mkdir()
    monkeypatch.setattr(git_router, "_run_git", _fake_git(" M first.txt\0?? sp ace.txt\0"))
    monkeypatch.setattr(git_router, "get_branch", lambda _ws: "main")
    data = c.get("/api/git/status").json()
    assert data["files"] == [
        {"path": "first.txt", "status": "M"},
        {"path": "sp ace.txt", "status": "?"},
    ]
    assert (data["changed"], data["untracked"]) == (1, 1)


def test_changes_against_real_git(client):
    """Real git output: the first entry is unstaged, one path has a space, one
    file is renamed. Paths must match the files on disk so the panel can open,
    diff, and restore them."""
    c, repo = client
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    for name in ("first.txt", "old.txt", "sp ace.txt"):
        (repo / name).write_text("1\n")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "c1"], repo)

    (repo / "first.txt").write_text("2\n")
    (repo / "sp ace.txt").write_text("2\n")
    _git(["mv", "old.txt", "new.txt"], repo)
    (repo / "staged.txt").write_text("1\n")
    _git(["add", "staged.txt"], repo)

    files = c.get("/api/git/changes").json()["files"]
    assert files[0]["path"] == "first.txt"
    assert {f["path"]: (f["status"], f["staged"]) for f in files} == {
        "first.txt": ("M", False),
        "new.txt": ("R", True),
        "sp ace.txt": ("M", False),
        "staged.txt": ("A", True),
    }
    for f in files:
        assert (repo / f["path"]).is_file()
