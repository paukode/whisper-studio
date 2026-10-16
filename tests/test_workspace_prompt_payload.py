"""The [WS_WORKSPACE_PROMPT] payload behind the folder picker.

The picker fires for one reason only: a tool that modifies or deletes an
existing file ran with no workspace connected, so its relative path has no
root. A path outside a CONNECTED workspace never raises it (see
tests/test_scratch_folder_access.py): the workspace was fine, the path was
not, and offering to switch roots cannot fix a path.

The recents pruning matters because dead entries (a pytest temp workspace that
leaked into the recents file outlives the run that made it) were offered as
buttons, and the FIRST one is promoted as the card's primary suggestion, so an
unpruned list hands the user a button that can only fail.
"""

import json
import os

from server.workspace import state


def _payload(tool_input: dict) -> dict:
    out = state._workspace_prompt_payload("ws_write_file", tool_input)
    assert out.startswith("[WS_WORKSPACE_PROMPT]")
    return json.loads(out[len("[WS_WORKSPACE_PROMPT]") :])


def _write_recents(paths: list[str]) -> None:
    os.makedirs(os.path.dirname(state.RECENT_WORKSPACES_PATH), exist_ok=True)
    with open(state.RECENT_WORKSPACES_PATH, "w") as f:
        json.dump(paths, f)


def test_payload_names_the_tool_and_carries_its_input(monkeypatch):
    monkeypatch.setattr(state, "get_workspace_path", lambda: None)

    payload = _payload({"path": "notes.txt", "content": "x", "__session_id__": "s"})

    assert payload["tool_name"] == "ws_write_file"
    assert payload["tool_input"] == {"path": "notes.txt", "content": "x"}


def test_recents_drop_folders_that_no_longer_exist(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    dead = tmp_path / "pytest-of-someone" / "ws"  # never created
    monkeypatch.setattr(state, "get_workspace_path", lambda: None)
    _write_recents([str(dead), str(live)])

    payload = _payload({"path": "notes.txt"})

    assert payload["recent"] == [str(live)]
    # The primary button is recents[0]; it must not be the dead folder.
    assert payload["suggested"] == str(live)


def test_suggested_falls_back_to_documents_when_nothing_is_offerable(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "get_workspace_path", lambda: None)
    _write_recents([str(tmp_path / "gone")])

    payload = _payload({"path": "notes.txt"})

    assert payload["recent"] == []
    assert payload["suggested"] == os.path.expanduser("~/Documents")
