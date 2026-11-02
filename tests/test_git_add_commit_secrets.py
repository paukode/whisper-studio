"""git_add_commit with all=true stages new files too (``git add -A``), so
the secret check must see untracked files, not only tracked changes."""

import subprocess

import pytest

from server.git import executor as gx


def _run(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _run(tmp_path, "init", "-q", "-b", "main")
    _run(tmp_path, "config", "user.email", "t@example.com")
    _run(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n")
    _run(tmp_path, "add", "a.txt")
    _run(tmp_path, "commit", "-q", "-m", "first")
    monkeypatch.setattr(gx, "get_workspace_path", lambda: str(tmp_path))
    return tmp_path


def test_an_untracked_secret_file_is_refused_with_all(repo):
    (repo / "a.txt").write_text("two\n")
    (repo / ".env").write_text("TOKEN=x\n")

    ok, out = gx.do_git_add_commit({"message": "m", "all": True})

    assert not ok
    assert ".env" in out
    assert _run(repo, "diff", "--cached", "--name-only").stdout == ""
    assert _run(repo, "log", "--oneline").stdout.count("\n") == 1


def test_an_ignored_secret_file_does_not_block_all(repo):
    (repo / ".gitignore").write_text(".env\n")
    (repo / ".env").write_text("TOKEN=x\n")

    ok, out = gx.do_git_add_commit({"message": "m", "all": True})

    assert ok, out
    assert ".env" not in _run(repo, "show", "--name-only", "HEAD").stdout


def test_new_files_are_committed_with_all(repo):
    (repo / "b.txt").write_text("new\n")

    ok, out = gx.do_git_add_commit({"message": "m", "all": True})

    assert ok, out
    assert "b.txt" in _run(repo, "show", "--name-only", "HEAD").stdout
