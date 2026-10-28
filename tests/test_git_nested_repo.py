"""Git read tools in a workspace that is the parent of several projects.

In a field session the workspace was ~/experiment, with each project its
own repository inside it. Every git tool said "Workspace is not a git
repository", and the read-only reviewer agents (no shell) could not see a
diff at all. The read tools now take ``repo``, a folder inside the
workspace, and the error names the repositories there.
"""

from __future__ import annotations

import subprocess

import pytest

from server.git import executor as gx
from server.git.tools import GIT_READ_TOOLS, GIT_WRITE_TOOLS


def _repo(path) -> None:
    path.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (path / "a.txt").write_text("one\n")
    run("add", "a.txt")
    run("commit", "-q", "-m", "first")


@pytest.fixture
def parent(tmp_path, monkeypatch):
    _repo(tmp_path / "app")
    _repo(tmp_path / "site")
    (tmp_path / "notes").mkdir()
    monkeypatch.setattr(gx, "get_workspace_path", lambda: str(tmp_path))
    return tmp_path


def test_every_read_tool_offers_repo_and_no_write_tool_does():
    assert all("repo" in t["input_schema"]["properties"] for t in GIT_READ_TOOLS)
    assert not any("repo" in t["input_schema"].get("properties", {}) for t in GIT_WRITE_TOOLS)


def test_the_error_names_the_repositories_inside_the_workspace(parent):
    out = gx._exec_git_status({}, "", None)
    assert out.startswith("Workspace is not a git repository. Repositories inside it: app, site.")
    assert "repo='app'" in out
    # A write tool cannot take repo, so it says how to change one instead.
    _cwd, err = gx._get_git_cwd()
    assert "git -C <folder> through ws_run_command" in err


def test_a_read_tool_reads_the_named_repository(parent):
    (parent / "app" / "a.txt").write_text("two\n")
    assert "a.txt" in gx._exec_git_status({"repo": "app", "short": True}, "", None)
    assert "+two" in gx._exec_git_diff({"repo": "app"}, "", None)
    assert "first" in gx._exec_git_log({"repo": str(parent / "site")}, "", None)


def test_repo_stays_inside_the_workspace_and_must_be_a_repository(parent, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    _repo(outside / "other")
    assert "inside the workspace" in gx._exec_git_status({"repo": str(outside / "other")}, "", None)
    assert "inside the workspace" in gx._exec_git_status({"repo": "../"}, "", None)
    assert gx._exec_git_status({"repo": "notes"}, "", None) == (
        "notes is not a git repository (it has no .git)."
    )
