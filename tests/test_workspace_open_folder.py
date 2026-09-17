"""ws_open_folder: resolves names (typed or spoken), offers candidates instead of
guessing, switches workspaces only when asked, and never creates a folder the
user did not ask for."""

import json
import os

import pytest

from server.workspace import executors as E


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Workspace state pinned to tmp, folder lookup roots pinned to tmp/home."""
    import server.workspace.state as S
    from server.security import folder_grants as fg
    from server.workspace import folder_lookup as FL

    monkeypatch.setattr(S, "WORKSPACE_CONFIG_PATH", str(tmp_path / "workspace.json"))
    monkeypatch.setattr(S, "RECENT_WORKSPACES_PATH", str(tmp_path / "recent.json"))
    monkeypatch.setattr(E, "load_workspace_config", S.load_workspace_config)
    monkeypatch.setattr(E, "save_workspace_config", S.save_workspace_config)
    monkeypatch.setattr(E, "save_recent_workspace", S.save_recent_workspace)
    monkeypatch.setattr(E, "get_workspace_path", S.get_workspace_path)
    monkeypatch.setattr(fg, "GRANTS_PATH", str(tmp_path / "grants.json"))
    home = tmp_path / "home"
    (home / "Documents" / "ml-ops").mkdir(parents=True)
    (home / "Documents" / "ml-reports").mkdir()
    (home / "Documents" / "notes").mkdir()
    monkeypatch.setattr(FL, "DEFAULT_ROOTS", (str(home), str(home / "Documents")))
    return S, home


def _grant_all(monkeypatch):
    monkeypatch.setattr("server.security.folder_grants.needs_grant", lambda *a, **k: False)


def test_spoken_name_resolves_and_opens(ws, monkeypatch):
    S, home = ws
    _grant_all(monkeypatch)
    out = json.loads(E.execute_ws_open_folder({"path": "m l dash o p s folder in my documents"}))
    assert out["opened"] is True
    assert out["path"] == os.path.realpath(str(home / "Documents" / "ml-ops"))
    assert S.get_workspace_path() == out["path"]


def test_close_matches_are_returned_for_confirmation(ws, monkeypatch):
    S, home = ws
    _grant_all(monkeypatch)
    out = json.loads(E.execute_ws_open_folder({"path": "ml"}))
    assert "candidates" in out
    assert {os.path.basename(c) for c in out["candidates"]} == {"ml-ops", "ml-reports"}
    assert "ask_user_question" in out["hint"]
    assert S.get_workspace_path() is None
    assert not (home / "Documents" / "ml").exists()


def test_bare_unknown_name_is_not_created_and_real_names_are_offered(ws, monkeypatch):
    S, home = ws
    _grant_all(monkeypatch)
    out = json.loads(E.execute_ws_open_folder({"path": "zzz-nothing-like-it"}))
    assert "No folder matching" in out["error"]
    # What actually exists there, so the model asks with real names.
    assert set(out["available"]) >= {"ml-ops", "ml-reports", "notes"}
    assert "Never invent" in out["hint"]
    assert S.get_workspace_path() is None
    assert not list(home.glob("**/zzz*"))


def test_a_misheard_word_still_offers_the_related_folders(ws, monkeypatch):
    S, home = ws
    _grant_all(monkeypatch)
    (home / "Documents" / "orders-etl").mkdir()
    out = json.loads(E.execute_ws_open_folder({"path": "flight-etl folder in my documents"}))
    assert [os.path.basename(c) for c in out["candidates"]] == ["orders-etl"]
    assert S.get_workspace_path() is None


def test_explicit_new_path_is_not_created_without_create_flag(ws, monkeypatch):
    """A misheard name arriving as a full path must not become an approval card
    for a folder nobody asked for; creation needs create=true."""
    S, home = ws
    target = home / "Desktop" / "brand-new"
    out = json.loads(E.execute_ws_open_folder({"path": str(target)}))
    assert "No folder matching" in out["error"] and "available" in out
    assert not target.exists()
    # With create=true the ungranted new folder asks first (folder_access).
    out = E.execute_ws_open_folder({"path": str(target), "create": "true"})
    assert out.startswith("[WS_APPROVAL]")
    payload = json.loads(out[len("[WS_APPROVAL]") :])
    assert payload["action"] == "folder_access" and payload["create"] is True
    assert payload["path"] == os.path.realpath(str(target))


def test_switching_requires_the_user_to_have_asked(ws, monkeypatch):
    S, home = ws
    _grant_all(monkeypatch)
    first = json.loads(E.execute_ws_open_folder({"path": "notes"}))
    assert first["opened"] and S.get_workspace_path().endswith("notes")
    blocked = json.loads(E.execute_ws_open_folder({"path": "ml-ops"}))
    assert "already connected" in blocked["error"] and "switch=true" in blocked["error"]
    assert blocked["resolved_path"].endswith("ml-ops")
    assert S.get_workspace_path().endswith("notes")
    switched = json.loads(E.execute_ws_open_folder({"path": "ml-ops", "switch": "true"}))
    assert switched["opened"] and S.get_workspace_path().endswith("ml-ops")
