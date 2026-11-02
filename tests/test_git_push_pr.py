"""git_push_pr reports a PR that was not created as a failure, and creates
it through run_gh (the hardened gh environment and secret scrubbing)."""

import pytest

from server.git import executor as gx
from server.git import gh_common


@pytest.fixture
def pushed(monkeypatch, tmp_path):
    monkeypatch.setattr(gx, "_get_git_cwd", lambda *a: (str(tmp_path), None))
    monkeypatch.setattr(gx, "get_branch", lambda cwd: "feature")
    monkeypatch.setattr(gx, "get_default_branch", lambda cwd: "main")
    monkeypatch.setattr(gx, "_git", lambda args, cwd, timeout=None: ("", "pushed", 0))
    return tmp_path


def test_a_failed_pr_create_is_not_reported_as_success(pushed, monkeypatch):
    monkeypatch.setattr(
        gh_common,
        "run_gh",
        lambda *a, **k: (4, "To get started with GitHub CLI, please run:  gh auth login"),
    )

    ok, out = gx.do_git_push_pr({"title": "t"})

    assert not ok
    assert "Pushed to origin/feature, but the PR was not created" in out
    assert "gh auth login" in out


def test_the_pr_is_created_through_run_gh(pushed, monkeypatch):
    seen = {}

    def fake(args, *, cwd, timeout, approved):
        seen.update(args=args, cwd=cwd, approved=approved)
        return 0, "https://github.com/o/r/pull/1"

    monkeypatch.setattr(gh_common, "run_gh", fake)

    ok, out = gx.do_git_push_pr({"title": "t", "body": "b", "base": "main"})

    assert ok
    assert out.endswith("https://github.com/o/r/pull/1")
    assert seen == {
        "args": ["pr", "create", "--title", "t", "--body", "b", "--base", "main"],
        "cwd": str(pushed),
        "approved": True,
    }
