"""The [WS_WORKSPACE_PROMPT] payload has to explain itself.

The card built from this payload is the one users see when a write pauses, and
for a long time it could only render the bare `reason` slug. A user staring at
a workspace panel that still showed the folder they connected had no way to
tell that the server's root had moved underneath them — the payload simply did
not carry the root it checked against or the path it rejected.

The recents pruning matters for the same reason: dead entries (a pytest temp
workspace that leaked into the recents file outlives the run that made it) were
offered as buttons, and the FIRST one is promoted as the card's primary
suggestion, so an unpruned list hands the user a button that can only fail.
"""

import json
import os

from server.workspace import state


def _payload(tool_input: dict, reason: str) -> dict:
    out = state._workspace_prompt_payload("ws_write_file", tool_input, reason)
    assert out.startswith("[WS_WORKSPACE_PROMPT]")
    return json.loads(out[len("[WS_WORKSPACE_PROMPT]") :])


def _write_recents(paths: list[str]) -> None:
    os.makedirs(os.path.dirname(state.RECENT_WORKSPACES_PATH), exist_ok=True)
    with open(state.RECENT_WORKSPACES_PATH, "w") as f:
        json.dump(paths, f)


def test_outside_workspace_payload_names_the_root_and_the_rejected_path(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(state, "get_workspace_path", lambda: str(ws))

    payload = _payload({"path": "../escape.txt", "content": "x"}, "outside_workspace")

    assert payload["workspace"] == str(ws)
    assert payload["attempted"] == os.path.realpath(str(tmp_path / "escape.txt"))


def test_no_workspace_payload_has_no_attempted_path(tmp_path, monkeypatch):
    """With nothing connected there is no root to have been outside of, and the
    tool's relative path has no absolute form to show."""
    monkeypatch.setattr(state, "get_workspace_path", lambda: None)

    payload = _payload({"path": "notes.txt", "content": "x"}, "no_workspace")

    assert payload["workspace"] is None
    assert payload["attempted"] is None


def test_recents_drop_folders_that_no_longer_exist(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    dead = tmp_path / "pytest-of-someone" / "ws"  # never created
    monkeypatch.setattr(state, "get_workspace_path", lambda: None)
    _write_recents([str(dead), str(live)])

    payload = _payload({"path": "notes.txt"}, "no_workspace")

    assert payload["recent"] == [str(live)]
    # The primary button is recents[0]; it must not be the dead folder.
    assert payload["suggested"] == str(live)


def test_recents_exclude_the_currently_connected_workspace(tmp_path, monkeypatch):
    """Offering the root that is already connected is a no-op button: it
    reconnects the same folder and changes nothing about why the write failed.
    Keeping it out leaves the picker to genuine alternatives."""
    ws = tmp_path / "ws"
    ws.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(state, "get_workspace_path", lambda: str(ws))
    _write_recents([str(ws), str(other)])

    payload = _payload({"path": "../escape.txt"}, "outside_workspace")

    assert payload["recent"] == [str(other)]


def test_suggested_falls_back_to_documents_when_nothing_is_offerable(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "get_workspace_path", lambda: None)
    _write_recents([str(tmp_path / "gone")])

    payload = _payload({"path": "notes.txt"}, "no_workspace")

    assert payload["recent"] == []
    assert payload["suggested"] == os.path.expanduser("~/Documents")
