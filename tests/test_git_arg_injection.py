"""Security tests for the read-only git executors in server/git/executor.py.

These executors (`git_log`, `git_show`, `git_blame`) append model-controlled
`branch`/`ref`/`file` values to the git argv. Because git treats option-like
arguments as flags, an unguarded value such as ``--output=/path`` would let the
model make git write to an arbitrary file — an unapproved arbitrary file write.

The fix has two layers:
  * refs/branches (which sit *before* the ``--`` separator) are validated with
    ``is_safe_ref_name`` and rejected when option-like;
  * pathspecs / files are placed *after* a literal ``--`` so git can never
    reparse them as flags; and the log ``limit`` is coerced to a bounded int so
    ``-{limit}`` cannot smuggle a flag.

Tests are hermetic: each uses a throwaway git repo under ``tmp_path`` and points
the executor at it by monkeypatching ``get_workspace_path``.
"""

import subprocess

import pytest

import server.git.executor as gx
from server.git.executor import (
    _coerce_limit,
    _exec_git_blame,
    _exec_git_log,
    _exec_git_show,
)


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A real one-commit git repo wired up as the active workspace."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def _run(*args):
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    _run("init")
    _run("config", "user.email", "tester@example.com")
    _run("config", "user.name", "Tester")
    (repo / "a.txt").write_text("hello\nworld\n")
    _run("add", "a.txt")
    _run("commit", "-m", "init commit")

    # Both the executor's own reference and the one it re-imports resolve here.
    monkeypatch.setattr(gx, "get_workspace_path", lambda: str(repo))
    return repo


def _capture_git(monkeypatch):
    """Replace ``_git`` with a stub that records argv and returns clean output.

    Returns the list that receives each invocation's args so a test can assert
    on the exact argv git would have been handed.
    """
    calls = []

    def fake_git(args, cwd, timeout=15):
        calls.append(list(args))
        return "", "", 0

    monkeypatch.setattr(gx, "_git", fake_git)
    return calls


# --- limit coercion -------------------------------------------------------


def test_coerce_limit_clamps_and_defaults():
    assert _coerce_limit(20) == 20
    assert _coerce_limit("5") == 5
    assert _coerce_limit(0) == 1  # clamped up to the floor
    assert _coerce_limit(10_000) == 1000  # clamped down to the ceiling
    # Non-int / option-like garbage falls back to the default rather than
    # flowing into ``-{limit}`` as a flag.
    assert _coerce_limit("-output=/tmp/x") == 20
    assert _coerce_limit(None) == 20
    assert _coerce_limit("abc") == 20


# --- git_show -------------------------------------------------------------


def test_git_show_rejects_option_injection_ref(git_repo, tmp_path):
    sentinel = tmp_path / "PWNED_SHOW"
    out = _exec_git_show({"ref": f"--output={sentinel}"}, [], [])
    assert out.startswith("Error: invalid ref name:")
    # The malicious ref never reached git, so no file was written.
    assert not sentinel.exists()


def test_git_show_legit_ref_produces_normal_output(git_repo):
    out = _exec_git_show({"ref": "HEAD"}, [], [])
    assert "init commit" in out
    assert "a.txt" in out  # the diff for the committed file is shown


def test_git_show_default_ref_is_head(git_repo):
    out = _exec_git_show({}, [], [])
    assert "init commit" in out


def test_git_show_argv_has_separator_and_validated_ref(monkeypatch, git_repo):
    calls = _capture_git(monkeypatch)
    _exec_git_show({"ref": "HEAD", "stat": True}, [], [])
    assert calls, "git should have been invoked for a legitimate ref"
    argv = calls[0]
    assert argv[0] == "show"
    assert "--" in argv  # end-of-options separator present
    assert "HEAD" in argv
    # No unguarded option-like token before the separator.
    sep = argv.index("--")
    assert not any(tok.startswith("--output") for tok in argv[:sep])


# --- git_log --------------------------------------------------------------


def test_git_log_rejects_option_injection_branch(git_repo, tmp_path):
    sentinel = tmp_path / "PWNED_LOG"
    out = _exec_git_log({"branch": f"--output={sentinel}"}, [], [])
    assert out.startswith("Error: invalid ref name:")
    assert not sentinel.exists()


def test_git_log_bad_limit_falls_back_and_writes_nothing(git_repo, tmp_path):
    sentinel = tmp_path / "PWNED_LIMIT"
    # A non-int limit must never become ``-{limit}`` as a flag; it falls back
    # to the default and produces normal log output.
    out = _exec_git_log({"limit": f"-output={sentinel}"}, [], [])
    assert not out.startswith("Error:")
    assert "init commit" in out
    assert not sentinel.exists()


def test_git_log_legit_branch_and_file(git_repo):
    out = _exec_git_log({"branch": "HEAD", "file": "a.txt"}, [], [])
    assert "init commit" in out


def test_git_log_argv_validates_branch_and_positions_file(monkeypatch, git_repo):
    calls = _capture_git(monkeypatch)
    _exec_git_log({"limit": 5, "branch": "main", "file": "a.txt"}, [], [])
    assert calls
    argv = calls[0]
    assert argv[0] == "log"
    assert argv[1] == "-5"  # coerced, bounded int
    assert "--" in argv
    sep = argv.index("--")
    # branch precedes the separator (validated), file follows it (pathspec).
    assert "main" in argv[:sep]
    assert "a.txt" in argv[sep + 1 :]


# --- git_blame ------------------------------------------------------------


def test_git_blame_option_injection_file_is_neutralized(git_repo, tmp_path):
    sentinel = tmp_path / "PWNED_BLAME"
    out = _exec_git_blame({"file": f"--output={sentinel}"}, [], [])
    # After ``--`` git treats the value as a (nonexistent) pathspec and errors
    # out; crucially it never wrote the sentinel file.
    assert out.startswith("Error:")
    assert not sentinel.exists()


def test_git_blame_legit_file_produces_output(git_repo):
    out = _exec_git_blame({"file": "a.txt"}, [], [])
    assert "hello" in out
    assert "world" in out


def test_git_blame_line_range_still_works(git_repo):
    out = _exec_git_blame({"file": "a.txt", "line_start": 1, "line_end": 1}, [], [])
    assert "hello" in out
    assert "world" not in out


def test_git_blame_argv_puts_file_after_separator(monkeypatch, git_repo):
    calls = _capture_git(monkeypatch)
    _exec_git_blame({"file": "a.txt", "line_start": 1, "line_end": 2}, [], [])
    assert calls
    argv = calls[0]
    assert argv[0] == "blame"
    assert "--" in argv
    sep = argv.index("--")
    assert argv[sep + 1 :] == ["a.txt"]
