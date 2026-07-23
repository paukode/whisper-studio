"""Security tests for `_exec_ws_merge_worktree` in server/workspace/executors.py.

Regression guard for a command-injection fix. The merge executor used to emit
a shell `{"action": "command", "command": f"git merge --no-ff {branch} ..."}`
payload that later ran through a shell. Git ref names permit shell-active
characters without spaces (brace expansion `{a,b}`, `;`, `(`) and the message
was single-quoted, so a crafted worktree name yielded command injection (or, at
minimum, broke the `-m '...'` quoting).

The fix runs the merge with ARGV — `["git", "merge", "--no-ff", branch, "-m",
msg]` via subprocess.run — the same argv approach do_git_merge and the sibling
worktree ops (ws_create_worktree, ws_diff_worktree) use. These tests assert the
metacharacter branch/name reaches git as one literal argument, never a shell
string, and that a normal branch still merges end-to-end.
"""

import json
import subprocess

import pytest

from server.workspace import executors
from server.workspace.executors import _exec_ws_merge_worktree


@pytest.fixture(autouse=True)
def _clear_worktrees():
    """Keep the in-memory worktree registry from leaking across tests."""
    executors._WORKTREES.clear()
    yield
    executors._WORKTREES.clear()


class _FakeCompleted:
    def __init__(self, argv):
        self.args = argv
        self.returncode = 0
        self.stdout = "Merge made by the 'ort' strategy.\n"
        self.stderr = ""


def _capture_subprocess(monkeypatch):
    """Replace subprocess.run in the executor module with an argv recorder.

    Returns the list of {"argv", "kwargs"} records so a test can assert on the
    exact argv git would have received and that no shell was requested.
    """
    calls = []

    def fake_run(args, **kwargs):
        calls.append({"argv": list(args), "kwargs": kwargs})
        return _FakeCompleted(list(args))

    monkeypatch.setattr(executors.subprocess, "run", fake_run)
    return calls


# --- argv, no shell interpolation ----------------------------------------


def test_merge_worktree_metacharacter_name_is_single_argv_arg(tmp_path, monkeypatch):
    """A branch/name full of shell metacharacters must be one literal argv arg."""
    monkeypatch.setattr(executors, "get_workspace_path", lambda: str(tmp_path))
    calls = _capture_subprocess(monkeypatch)

    evil_name = "x;{touch,/tmp/pwned}"
    evil_branch = f"whisper/{evil_name}"
    executors._WORKTREES[evil_name] = {
        "path": str(tmp_path / ".worktrees" / "x"),
        "branch": evil_branch,
        "base": "HEAD",
    }

    out = _exec_ws_merge_worktree({"name": evil_name}, [], [])

    # No shell `[WS_APPROVAL]` command payload is emitted any more.
    assert not out.startswith("[WS_APPROVAL]")

    assert calls, "git should have been invoked via subprocess.run"
    record = calls[0]
    argv, kwargs = record["argv"], record["kwargs"]

    # Argv form, and crucially never shell=True.
    assert kwargs.get("shell") in (None, False)
    assert argv[0] == "git"
    assert argv[1] == "merge"
    assert "--no-ff" in argv

    # The metacharacter branch is exactly one literal element — never split on
    # `;`, never glued to another token, never expanded.
    assert argv.count(evil_branch) == 1
    # No argv element is itself a composite shell command line.
    assert not any(tok != "git" and "git merge" in tok for tok in argv)

    # The message rides its own `-m` argument (no fragile single-quoting). It
    # is allowed to carry the metacharacters verbatim precisely because it is a
    # single literal arg the shell never parses.
    m = argv.index("-m")
    assert argv[m + 1] == f"Merge worktree {evil_name}"

    # Every argv element is a plain string; the metacharacters live only inside
    # the two elements that legitimately embed the name (branch + message), and
    # nowhere are they concatenated with the `git`/`merge`/`--no-ff` tokens.
    for tok in (argv[0], argv[1], "--no-ff", "-m"):
        assert ";" not in tok and "{" not in tok

    # cwd is the workspace; nothing was run through a shell string.
    assert kwargs.get("cwd") == str(tmp_path)


def test_merge_worktree_single_quote_name_does_not_break(tmp_path, monkeypatch):
    """A single quote in the name used to break the `-m '...'` quoting."""
    monkeypatch.setattr(executors, "get_workspace_path", lambda: str(tmp_path))
    calls = _capture_subprocess(monkeypatch)

    name = "it's-a-branch"
    branch = f"whisper/{name}"
    executors._WORKTREES[name] = {
        "path": str(tmp_path / ".worktrees" / name),
        "branch": branch,
        "base": "HEAD",
    }

    out = _exec_ws_merge_worktree({"name": name}, [], [])
    assert not out.startswith("[WS_APPROVAL]")
    argv = calls[0]["argv"]
    assert branch in argv
    m = argv.index("-m")
    # The quote survives intact as a plain argv element — no shell parsing.
    assert argv[m + 1] == f"Merge worktree {name}"


def test_merge_worktree_unknown_name_reports_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(executors, "get_workspace_path", lambda: str(tmp_path))
    calls = _capture_subprocess(monkeypatch)
    out = _exec_ws_merge_worktree({"name": "nope"}, [], [])
    assert "not found" in out
    assert not calls, "git must not run for an unknown worktree"


# --- real end-to-end merge ------------------------------------------------


def _git(repo, *args):
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def git_repo(tmp_path):
    """A real repo with one base commit, wired for direct git execution."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tester@example.com")
    _git(repo, "config", "user.name", "Tester")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base commit")
    return repo


def test_merge_worktree_normal_branch_merges(git_repo, monkeypatch):
    """A legitimate worktree branch merges cleanly through the real argv path."""
    monkeypatch.setattr(executors, "get_workspace_path", lambda: str(git_repo))

    name = "feature"
    branch = f"whisper/{name}"
    wt_path = git_repo / ".worktrees" / name
    _git(git_repo, "worktree", "add", "-b", branch, str(wt_path))
    (wt_path / "feature.txt").write_text("from the worktree\n")
    _git(wt_path, "add", "feature.txt")
    _git(wt_path, "commit", "-m", "add feature file")

    executors._WORKTREES[name] = {
        "path": str(wt_path),
        "branch": branch,
        "base": "HEAD",
    }

    out = _exec_ws_merge_worktree({"name": name}, [], [])
    payload = json.loads(out)
    assert payload["merged"] is True
    assert payload["branch"] == branch
    # The branch's file is now present in the main worktree — the merge really
    # happened, with the descriptive commit message intact.
    assert (git_repo / "feature.txt").read_text() == "from the worktree\n"
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == f"Merge worktree {name}"


def test_merge_worktree_injection_name_does_not_execute_shell(git_repo, monkeypatch, tmp_path):
    """A crafted name must never spawn a shell side effect, even on failure."""
    monkeypatch.setattr(executors, "get_workspace_path", lambda: str(git_repo))

    sentinel = tmp_path / "PWNED"
    # A shell would run `touch <sentinel>` from the `;`/brace payload; argv
    # execution just hands git a branch it cannot resolve.
    evil_name = f"x;touch {sentinel}"
    evil_branch = f"whisper/{evil_name}"
    executors._WORKTREES[evil_name] = {
        "path": str(git_repo / ".worktrees" / "x"),
        "branch": evil_branch,
        "base": "HEAD",
    }

    out = _exec_ws_merge_worktree({"name": evil_name}, [], [])
    # Merge fails because the branch does not exist — but no shell ran.
    assert out.startswith("Failed to merge worktree")
    assert not out.startswith("[WS_APPROVAL]")
    assert not sentinel.exists(), "shell metacharacters must not be executed"
