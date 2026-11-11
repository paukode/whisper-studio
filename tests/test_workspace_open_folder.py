"""ws_open_folder: resolves names (typed or spoken), offers candidates instead of
guessing, switches workspaces only when asked, never creates a folder the user
did not ask for, and opens a folder outside the workspace only through the
folder_access card, every time."""

import asyncio
import json
import os

import pytest

from server.approval.executors import _do_folder_access
from server.workspace import executors as E


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Workspace state pinned to tmp, folder lookup roots pinned to tmp/home."""
    import server.workspace.state as S
    from server.workspace import folder_lookup as FL

    monkeypatch.setattr(S, "WORKSPACE_CONFIG_PATH", str(tmp_path / "workspace.json"))
    monkeypatch.setattr(S, "RECENT_WORKSPACES_PATH", str(tmp_path / "recent.json"))
    monkeypatch.setattr(E, "get_workspace_path", S.get_workspace_path)
    home = tmp_path / "home"
    (home / "Documents" / "ml-ops").mkdir(parents=True)
    (home / "Documents" / "ml-reports").mkdir()
    (home / "Documents" / "notes").mkdir()
    monkeypatch.setattr(FL, "DEFAULT_ROOTS", (str(home), str(home / "Documents")))
    return S, home


def _card(out: str) -> dict:
    assert out.startswith("[WS_APPROVAL]"), out
    return json.loads(out[len("[WS_APPROVAL]") :])


def _approved(tool_input: dict) -> dict:
    """ws_open_folder, with a person clicking Yes on the folder_access card."""
    payload = _card(E.execute_ws_open_folder(tool_input))
    assert payload["action"] == "folder_access"
    outcome = asyncio.run(_do_folder_access(payload))
    assert outcome.ok, outcome.error
    return json.loads(outcome.output)


def test_spoken_name_resolves_and_opens(ws, monkeypatch):
    S, home = ws
    out = _approved({"path": "m l dash o p s folder in my documents"})
    assert out["opened"] is True
    assert out["path"] == os.path.realpath(str(home / "Documents" / "ml-ops"))
    assert S.get_workspace_path() == out["path"]


def test_close_matches_are_returned_for_confirmation(ws, monkeypatch):
    S, home = ws
    out = json.loads(E.execute_ws_open_folder({"path": "ml"}))
    assert "candidates" in out
    assert {os.path.basename(c) for c in out["candidates"]} == {"ml-ops", "ml-reports"}
    assert "ask_user_question" in out["hint"]
    assert S.get_workspace_path() is None
    assert not (home / "Documents" / "ml").exists()


def test_bare_unknown_name_is_not_created_and_real_names_are_offered(ws, monkeypatch):
    S, home = ws
    out = json.loads(E.execute_ws_open_folder({"path": "zzz-nothing-like-it"}))
    assert "No folder matching" in out["error"]
    # What actually exists there, so the model asks with real names.
    assert set(out["available"]) >= {"ml-ops", "ml-reports", "notes"}
    assert "Never invent" in out["hint"]
    assert S.get_workspace_path() is None
    assert not list(home.glob("**/zzz*"))


def test_a_misheard_word_still_offers_the_related_folders(ws, monkeypatch):
    S, home = ws
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
    # With create=true the new folder outside the workspace asks first.
    payload = _card(E.execute_ws_open_folder({"path": str(target), "create": "true"}))
    assert payload["action"] == "folder_access" and payload["create"] is True
    assert payload["path"] == os.path.realpath(str(target))


def test_switching_requires_the_user_to_have_asked(ws, monkeypatch):
    S, home = ws
    first = _approved({"path": "notes"})
    assert first["opened"] and S.get_workspace_path().endswith("notes")
    blocked = json.loads(E.execute_ws_open_folder({"path": "ml-ops"}))
    assert "already connected" in blocked["error"] and "switch=true" in blocked["error"]
    assert blocked["resolved_path"].endswith("ml-ops")
    assert S.get_workspace_path().endswith("notes")
    switched = _approved({"path": "ml-ops", "switch": "true"})
    assert switched["opened"] and S.get_workspace_path().endswith("ml-ops")


def test_an_approved_folder_is_not_remembered(ws, monkeypatch):
    """Nothing is remembered: after a switch away, opening the same folder
    shows the card again. Reopening the connected folder needs no card."""
    S, home = ws
    _approved({"path": "notes"})
    again = json.loads(E.execute_ws_open_folder({"path": "notes"}))
    assert again["opened"] is True
    _approved({"path": "ml-ops", "switch": "true"})
    payload = _card(E.execute_ws_open_folder({"path": "notes", "switch": "true"}))
    assert payload["path"] == os.path.realpath(str(home / "Documents" / "notes"))


def test_the_card_never_opens_a_credential_store_or_a_folder_holding_one(ws, monkeypatch):
    """A prompt-injected "open my home folder" is refused like "open ~/.ssh":
    a workspace exposes everything below it."""
    S, home = ws
    monkeypatch.setenv("HOME", str(home))
    (home / ".ssh").mkdir()
    for target in (home, home / ".ssh"):
        outcome = asyncio.run(_do_folder_access({"path": str(target), "create": True}))
        assert not outcome.ok and "protected" in outcome.error
    assert S.get_workspace_path() is None
    assert not (home / ".ssh" / "new").exists()


def test_an_unattended_agent_cannot_open_a_folder(ws):
    S, home = ws
    outcome = asyncio.run(
        _do_folder_access({"path": str(home / "Documents" / "notes"), "__agent__": True})
    )
    assert not outcome.ok
    assert S.get_workspace_path() is None


def test_open_folder_now_switches_like_a_ui_connect(ws, monkeypatch, tmp_path):
    """open_folder_now used to write the config itself: the git and file-tree
    watchers kept pointing at the old folder and the old cwds survived."""
    S, home = ws
    pointed: dict = {}
    cleared: list = []
    monkeypatch.setattr(S, "_point_git_watcher_at", lambda p: pointed.update(git=p))
    monkeypatch.setattr(S, "_point_workspace_watcher_at", lambda p: pointed.update(tree=p))
    monkeypatch.setattr(S, "clear_workspace_scoped_state", lambda: cleared.append(True))
    a = os.path.realpath(str(home / "Documents" / "notes"))
    b = os.path.realpath(str(home / "Documents" / "ml-ops"))
    S.connect_workspace(a)
    cleared.clear()

    out = json.loads(E.open_folder_now(b))

    assert out["opened"] is True and out["path"] == b
    assert S.get_workspace_path() == b
    assert pointed == {"git": b, "tree": b}
    assert cleared == [True]


def test_a_folder_the_app_may_not_read_is_refused(ws, monkeypatch):
    S, home = ws
    locked = home / "Documents" / "notes"
    real_listdir = os.listdir

    def listdir(p):
        if os.path.realpath(p) == os.path.realpath(str(locked)):
            raise PermissionError(1, "Operation not permitted", str(locked))
        return real_listdir(p)

    monkeypatch.setattr(os, "listdir", listdir)
    out = json.loads(E.open_folder_now(os.path.realpath(str(locked))))
    assert out["error"].startswith("Cannot read ")
    assert "Operation not permitted" in out["error"]
    assert S.get_workspace_path() is None
