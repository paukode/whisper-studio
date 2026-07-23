"""Tests for the GitHub tool executors: routing (read inline / mutation
sentinel / deny / redirect), the unattended-subagent refusal, and
verify-after-mutate. `run_gh` and the workspace are mocked — no gh or network.
"""

import json

import pytest

import server.git.gh_executor as gx


@pytest.fixture(autouse=True)
def _ws(monkeypatch):
    monkeypatch.setattr(gx, "get_workspace_path", lambda: "/tmp/repo")


def _mock_gh(monkeypatch, responder):
    monkeypatch.setattr(gx, "run_gh", lambda args, **kw: responder(args, kw))


# --- github verb tool routing ---


def test_read_runs_inline(monkeypatch):
    _mock_gh(monkeypatch, lambda args, kw: (0, "PR #1  OPEN"))
    out = gx._exec_github({"args": ["pr", "list"], "__session_id__": "s"}, None, None)
    assert out == "PR #1  OPEN"


def test_deny_family(monkeypatch):
    _mock_gh(monkeypatch, lambda args, kw: (0, "should not run"))
    out = gx._exec_github({"args": ["auth", "token"]}, None, None)
    assert out.startswith("Error") and "blocked" in out


def test_api_redirects(monkeypatch):
    _mock_gh(monkeypatch, lambda args, kw: (0, ""))
    out = gx._exec_github({"args": ["api", "repos/o/r"]}, None, None)
    assert out.startswith("Error") and "github_api" in out


def test_write_returns_sentinel():
    out = gx._exec_github(
        {"args": ["pr", "create", "--title", "x"], "__session_id__": "s"}, None, None
    )
    assert out.startswith("[WS_APPROVAL]")
    p = json.loads(out[len("[WS_APPROVAL]") :])
    assert p["action"] == "github" and p["args"] == ["pr", "create", "--title", "x"]


def test_danger_uses_destructive_category():
    out = gx._exec_github({"args": ["pr", "merge", "2"]}, None, None)
    p = json.loads(out[len("[WS_APPROVAL]") :])
    assert p["action"] == "github_destructive"


# --- do_github: agent refusal + verify ---


def test_do_github_refuses_agent(monkeypatch):
    _mock_gh(monkeypatch, lambda args, kw: (0, "closed"))
    ok, msg = gx.do_github({"args": ["pr", "close", "2"], "__agent__": True})
    assert ok is False and "unattended subagent" in msg


def test_do_github_verifies_state(monkeypatch):
    def responder(args, kw):
        if args[:2] == ["pr", "close"]:
            return 0, "closed"
        if args[:2] == ["pr", "view"]:
            return 0, json.dumps({"state": "CLOSED", "url": "u"})
        return 0, ""

    _mock_gh(monkeypatch, responder)
    ok, msg = gx.do_github({"args": ["pr", "close", "2"]})
    assert ok is True and "CLOSED" in msg


def test_do_github_flags_state_mismatch(monkeypatch):
    def responder(args, kw):
        if args[:2] == ["pr", "close"]:
            return 0, "closed"
        return 0, json.dumps({"state": "OPEN"})  # never became CLOSED

    _mock_gh(monkeypatch, responder)
    ok, msg = gx.do_github({"args": ["pr", "close", "2"]})
    assert ok is False and "expected CLOSED" in msg


def test_do_github_reports_gh_failure(monkeypatch):
    _mock_gh(monkeypatch, lambda args, kw: (1, "HTTP 403"))
    ok, msg = gx.do_github({"args": ["pr", "close", "2"]})
    assert ok is False and "403" in msg


# --- github_api plane ---


def test_api_read_runs_inline(monkeypatch):
    _mock_gh(monkeypatch, lambda args, kw: (0, '{"login":"x"}'))
    out = gx._exec_github_api({"endpoint": "user"}, None, None)
    assert out == '{"login":"x"}'


def test_api_read_rejects_write_method(monkeypatch):
    _mock_gh(monkeypatch, lambda args, kw: (0, ""))
    out = gx._exec_github_api({"endpoint": "repos/o/r/issues", "method": "POST"}, None, None)
    assert out.startswith("Error") and "github_api_write" in out


def test_api_read_rejects_denied_endpoint(monkeypatch):
    _mock_gh(monkeypatch, lambda args, kw: (0, ""))
    out = gx._exec_github_api({"endpoint": "repos/o/r/actions/secrets"}, None, None)
    assert out.startswith("Error")


def test_api_write_returns_sentinel():
    out = gx._exec_github_api_write(
        {
            "endpoint": "repos/o/r/issues",
            "method": "POST",
            "body": {"title": "x"},
            "__session_id__": "s",
        },
        None,
        None,
    )
    p = json.loads(out[len("[WS_APPROVAL]") :])
    assert p["action"] == "github_api_write" and p["endpoint"] == "repos/o/r/issues"


def test_api_write_delete_is_destructive():
    out = gx._exec_github_api_write(
        {"endpoint": "repos/o/r/git/refs/heads/x", "method": "DELETE"}, None, None
    )
    p = json.loads(out[len("[WS_APPROVAL]") :])
    assert p["action"] == "github_api_write_destructive"


def test_do_api_write_refuses_agent():
    ok, msg = gx.do_github_api_write(
        {"endpoint": "repos/o/r/issues", "method": "POST", "body": {}, "__agent__": True}
    )
    assert ok is False and "unattended subagent" in msg


def test_do_api_write_delete_verifies_gone(monkeypatch):
    calls = {"n": 0}

    def responder(args, kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return 0, ""  # the DELETE succeeds
        return 1, "HTTP 404"  # the re-GET 404s → gone

    _mock_gh(monkeypatch, responder)
    ok, msg = gx.do_github_api_write({"endpoint": "repos/o/r/git/refs/heads/x", "method": "DELETE"})
    assert ok is True and "deleted" in msg
