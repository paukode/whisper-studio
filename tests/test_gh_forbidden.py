"""The hard rule: no session, agent, or subagent may EVER delete/transfer a
repo, change its visibility, or add/remove repo collaborators or teams — not
even with approval. These are FORBIDDEN (never executed, never offered for
approval), a strictly harder block than DANGER.
"""

import pytest

import server.git.gh_common as gc
import server.git.gh_executor as gx
from server.git.gh_classify import FORBIDDEN, check_api_governance, classify_github


@pytest.fixture(autouse=True)
def _ws(monkeypatch):
    monkeypatch.setattr(gx, "get_workspace_path", lambda: "/tmp/repo")


# --- verb plane ---


@pytest.mark.parametrize(
    "args",
    [
        ["repo", "delete", "o/r"],
        ["repo", "transfer", "o/r"],
        ["repo", "edit", "--visibility", "public"],
        ["repo", "edit", "--visibility=private"],
        ["-R", "o/r", "repo", "delete"],
    ],
)
def test_verb_forbidden(args):
    assert classify_github(args).kind == FORBIDDEN


def test_repo_edit_without_visibility_is_normal_write():
    from server.git.gh_classify import WRITE

    assert classify_github(["repo", "edit", "--description", "hi"]).kind == WRITE


# --- API governance ---


@pytest.mark.parametrize(
    "method,endpoint,body",
    [
        ("DELETE", "repos/o/r", None),
        ("PATCH", "repos/o/r", {"private": False}),
        ("PATCH", "repos/o/r", {"visibility": "public"}),
        ("PUT", "repos/o/r/collaborators/bob", None),
        ("DELETE", "repos/o/r/collaborators/bob", None),
        ("POST", "repos/o/r/transfer", {"new_owner": "x"}),
        ("DELETE", "orgs/o/teams/eng", None),
        ("PUT", "orgs/o/teams/eng/repos/o/r", None),
        ("PUT", "orgs/o/memberships/bob", None),
    ],
)
def test_api_governance_forbids(method, endpoint, body):
    assert check_api_governance(method, endpoint, body) is not None


@pytest.mark.parametrize(
    "method,endpoint,body",
    [
        ("GET", "repos/o/r/collaborators", None),  # reads are fine
        ("GET", "orgs/o/teams", None),
        ("POST", "repos/o/r/issues", {"title": "x"}),  # normal write
        ("PATCH", "repos/o/r/pulls/2", {"title": "x"}),  # PR edit, not visibility
    ],
)
def test_api_governance_allows(method, endpoint, body):
    assert check_api_governance(method, endpoint, body) is None


# --- executor + runner enforcement (no approval sentinel, hard error) ---


def test_exec_github_blocks_forbidden(monkeypatch):
    monkeypatch.setattr(gx, "run_gh", lambda args, **kw: (0, "should not run"))
    out = gx._exec_github({"args": ["repo", "delete", "o/r"]}, None, None)
    assert out.startswith("Error") and "never permitted" in out
    assert "[WS_APPROVAL]" not in out


def test_do_github_refuses_forbidden_even_if_approved():
    ok, msg = gx.do_github({"args": ["repo", "delete", "o/r"]})
    assert ok is False and "never permitted" in msg


def test_exec_api_write_blocks_forbidden():
    out = gx._exec_github_api_write({"endpoint": "repos/o/r", "method": "DELETE"}, None, None)
    assert out.startswith("Error")
    assert "[WS_APPROVAL]" not in out


def test_do_api_write_refuses_collaborator():
    ok, msg = gx.do_github_api_write(
        {
            "endpoint": "repos/o/r/collaborators/bob",
            "method": "PUT",
            "body": {"permission": "admin"},
        }
    )
    assert ok is False


def test_run_gh_refuses_forbidden_even_approved():
    # Belt-and-suspenders: run_gh raises for a forbidden action regardless of approved.
    with pytest.raises(RuntimeError):
        gc.run_gh(["repo", "delete", "o/r"], cwd="/tmp/repo", approved=True)
