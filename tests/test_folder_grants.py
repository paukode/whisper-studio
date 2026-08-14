"""Ask-once, remember-after folder access.

User directive: when a path outside the connected workspace is named, ask for
permission to read it; once granted, remember it so the same place is never
asked about again.
"""

import asyncio
import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_grants(tmp_path, monkeypatch):
    from server.security import folder_grants as fg

    monkeypatch.setattr(fg, "GRANTS_PATH", str(tmp_path / "folder_grants.json"))
    yield


# ── the store itself ────────────────────────────────────────────────────────


def test_grant_then_no_longer_asks(tmp_path):
    from server.security.folder_grants import grant, needs_grant

    target = tmp_path / "project1"
    target.mkdir()
    assert needs_grant(str(target)) is True
    ok, err = grant(str(target))
    assert ok and err == ""
    assert needs_grant(str(target)) is False


def test_grant_covers_children_but_not_siblings_or_parent(tmp_path):
    from server.security.folder_grants import grant, needs_grant

    parent = tmp_path / "docs"
    child = parent / "project1"
    sibling = tmp_path / "downloads"
    child.mkdir(parents=True)
    sibling.mkdir()

    grant(str(parent))
    # Everything beneath the granted folder is covered — that is the point of
    # asking once at the level the user actually chose.
    assert needs_grant(str(child)) is False
    # A sibling was never approved.
    assert needs_grant(str(sibling)) is True
    # And granting a child must never imply its parent.
    from server.security.folder_grants import revoke

    revoke(str(parent))
    grant(str(child))
    assert needs_grant(str(child)) is False
    assert needs_grant(str(parent)) is True


def test_same_folder_by_different_spelling_is_one_grant(tmp_path, monkeypatch):
    from server.security.folder_grants import grant, needs_grant

    target = tmp_path / "project1"
    target.mkdir()
    grant(str(target))
    # A relative path resolving to the same place must not re-ask.
    monkeypatch.chdir(tmp_path)
    assert needs_grant("project1") is False
    assert needs_grant("./project1") is False


def test_tilde_path_is_expanded(tmp_path, monkeypatch):
    from server.security.folder_grants import grant, needs_grant

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    ok, _ = grant("~/Documents")
    assert ok
    assert needs_grant(os.path.join(str(tmp_path), "Documents")) is False


def test_workspace_itself_never_needs_a_grant(tmp_path):
    from server.security.folder_grants import needs_grant

    ws = tmp_path / "connected"
    (ws / "src").mkdir(parents=True)
    # Connecting the workspace WAS the approval — neither it nor anything
    # inside it should ever produce a second prompt.
    assert needs_grant(str(ws), workspace_path=str(ws)) is False
    assert needs_grant(str(ws / "src"), workspace_path=str(ws)) is False


def test_sensitive_paths_can_never_be_granted(tmp_path):
    from server.security.folder_grants import grant, is_sensitive

    assert is_sensitive("~/.ssh")
    assert is_sensitive("~/.aws")
    ok, err = grant("~/.ssh")
    assert ok is False
    assert "protected" in err


def test_home_itself_is_refused_because_it_contains_credentials():
    # A grant covers everything beneath it, so granting ~ would hand over
    # ~/.ssh by inheritance — refuse the ancestor too, not just the leaf.
    from server.security.folder_grants import grant, is_sensitive

    assert is_sensitive("~")
    assert grant("~")[0] is False
    assert grant("/")[0] is False


def test_missing_directory_is_refused(tmp_path):
    from server.security.folder_grants import grant

    ok, err = grant(str(tmp_path / "does-not-exist"))
    assert ok is False
    assert "does not exist" in err


def test_empty_path_never_silently_grants_the_servers_cwd(tmp_path, monkeypatch):
    """Caught in live testing: a malformed approval payload (no `path`) made
    realpath("") resolve to the server's own working directory, silently
    granting whatever folder the app happened to be running in."""
    from server.security.folder_grants import _canonical, grant, load_grants

    monkeypatch.chdir(tmp_path)
    assert _canonical("") == ""
    assert _canonical("   ") == ""
    for bad in ("", "   ", None):
        ok, err = grant(bad)
        assert ok is False
        assert "nothing to grant" in err
    assert load_grants() == []


def test_revoke_makes_it_ask_again(tmp_path):
    from server.security.folder_grants import grant, needs_grant, revoke

    target = tmp_path / "project1"
    target.mkdir()
    grant(str(target))
    assert needs_grant(str(target)) is False
    assert revoke(str(target)) is True
    assert needs_grant(str(target)) is True
    assert revoke(str(target)) is False  # nothing left to remove


def test_granting_a_parent_subsumes_existing_child_grants(tmp_path):
    from server.security.folder_grants import grant, load_grants

    parent = tmp_path / "docs"
    child = parent / "project1"
    child.mkdir(parents=True)
    grant(str(child))
    grant(str(parent))
    # The redundant child entry is dropped — the file stays a minimal set.
    assert [os.path.realpath(p) for p in load_grants()] == [os.path.realpath(str(parent))]


# ── ws_open_folder asks the first time, then stops asking ───────────────────


def test_ws_open_folder_requests_approval_for_an_ungranted_folder(tmp_path, monkeypatch):
    from server.workspace.executors import execute_ws_open_folder

    monkeypatch.setattr("server.workspace.executors.get_workspace_path", lambda: None)
    target = tmp_path / "project1"
    target.mkdir()

    out = execute_ws_open_folder({"path": str(target)})
    assert out.startswith("[WS_APPROVAL]")
    assert '"action": "folder_access"' in out


def test_ws_open_folder_proceeds_silently_once_granted(tmp_path, monkeypatch):
    import json as _json

    from server.security.folder_grants import grant
    from server.workspace.executors import execute_ws_open_folder

    monkeypatch.setattr("server.workspace.executors.get_workspace_path", lambda: None)
    monkeypatch.setattr("server.workspace.executors.save_workspace_config", lambda c: None)
    monkeypatch.setattr("server.workspace.executors.load_workspace_config", lambda: {})
    monkeypatch.setattr("server.workspace.executors.save_recent_workspace", lambda p: None)
    target = tmp_path / "project1"
    target.mkdir()

    grant(str(target))
    out = execute_ws_open_folder({"path": str(target)})
    assert not out.startswith("[WS_APPROVAL]")
    assert _json.loads(out)["opened"] is True


# ── the approval executor ───────────────────────────────────────────────────


def test_folder_access_executor_persists_the_grant(tmp_path):
    from server.approval.bootstrap import _do_folder_access
    from server.security.folder_grants import needs_grant

    target = tmp_path / "project1"
    target.mkdir()
    outcome = asyncio.run(_do_folder_access({"path": str(target)}))
    assert outcome.ok is True
    assert needs_grant(str(target)) is False


def test_folder_access_executor_refuses_an_unattended_agent(tmp_path):
    from server.approval.bootstrap import _do_folder_access
    from server.security.folder_grants import needs_grant

    target = tmp_path / "project1"
    target.mkdir()
    outcome = asyncio.run(_do_folder_access({"__agent__": True, "path": str(target)}))
    assert outcome.ok is False
    # And nothing was persisted — an agent cannot widen the app's reach.
    assert needs_grant(str(target)) is True


def test_folder_access_executor_refuses_sensitive_paths(tmp_path):
    from server.approval.bootstrap import _do_folder_access

    outcome = asyncio.run(_do_folder_access({"path": "~/.ssh"}))
    assert outcome.ok is False
    assert "protected" in (outcome.error or "")


# ── workflow pinning refuses an ungranted folder ────────────────────────────


def test_workflow_pin_refuses_an_ungranted_folder(tmp_path, monkeypatch):
    from server.workflows.manager import resolve_workflow_workspace

    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: None)
    target = tmp_path / "project1"
    target.mkdir()

    resolved, err = resolve_workflow_workspace(str(target))
    assert resolved is None
    assert "has not been approved" in err


def test_workflow_pin_accepts_a_granted_folder(tmp_path, monkeypatch):
    from server.security.folder_grants import grant
    from server.workflows.manager import resolve_workflow_workspace

    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: None)
    target = tmp_path / "project1"
    target.mkdir()
    grant(str(target))

    resolved, err = resolve_workflow_workspace(str(target))
    assert err == ""
    assert resolved == os.path.realpath(str(target))


def test_workflow_pin_inside_the_connected_workspace_needs_no_grant(tmp_path, monkeypatch):
    from server.workflows.manager import resolve_workflow_workspace

    ws = tmp_path / "connected"
    sub = ws / "sub"
    sub.mkdir(parents=True)
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(ws))

    resolved, err = resolve_workflow_workspace(str(sub))
    assert err == ""
    assert resolved == os.path.realpath(str(sub))
