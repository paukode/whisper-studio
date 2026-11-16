"""A session always has a working folder: with no workspace connected it is
the session's own scratch folder (the user's rule, 2026-11-16).

Before, a file tool called with no workspace raised the folder picker, the
command tool answered "No workspace connected", and a created file went to
~/Documents. A long run with no folder connected lost rounds to each of them.
Now a relative path resolves in the scratch folder, a command starts there,
and the system prompt tells the model to link the result and offer to open it
or to save it where the user wants.
"""

import asyncio
import json
import os

import pytest

from server.approval.executors import _do_command, _do_write
from server.prompts import _build_no_workspace_section
from server.workspace import executors, paths, state
from server.workspace.paths import get_scratch_dir

SID = "sess-no-ws"


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """No workspace connected; the scratch folder is under tmp_path."""
    monkeypatch.setattr(paths, "DATA_DIR", str(tmp_path / "data"))
    for target in (state, executors):
        monkeypatch.setattr(target, "get_workspace_path", lambda: None)
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    monkeypatch.setattr(state, "workspace_lost_message", lambda: None)
    monkeypatch.setattr("server.file_state.check_write_allowed", lambda *a, **k: (True, ""))
    # The session's remembered shell folder is from another test's tmp_path.
    from server import cwd_tracker

    cwd_tracker.clear_session(SID)
    return get_scratch_dir(SID)


def _call(fn, tool_input: dict) -> str:
    return fn({**tool_input, "__session_id__": SID}, [], [])


def _approval(out: str) -> dict:
    assert out.startswith("[WS_APPROVAL]"), out
    return json.loads(out[len("[WS_APPROVAL]") :])


def test_the_working_folder_is_the_workspace_else_the_scratch_folder(scratch, monkeypatch):
    assert state.work_root(SID) == scratch
    # No session owns a scratch folder, so there is no working folder.
    assert state.work_root("") is None
    monkeypatch.setattr(state, "get_workspace_path", lambda: "/some/ws")
    assert state.work_root(SID) == "/some/ws"


def test_a_turn_that_lost_its_workspace_is_not_moved_to_the_scratch_folder(scratch, monkeypatch):
    monkeypatch.setattr(state, "workspace_lost_message", lambda: "the workspace was disconnected")
    assert state.work_root(SID) is None


def test_a_new_file_with_no_workspace_is_made_in_the_scratch_folder(scratch):
    out = _call(executors._exec_ws_create_file, {"path": "build/model.py", "content": "x = 1\n"})
    payload = _approval(out)
    assert payload["action"] == "create" and payload["path"] == "build/model.py"

    outcome = asyncio.run(_do_write({**payload, "session_id": SID}))
    assert outcome.ok, outcome.error
    made = os.path.join(scratch, "build", "model.py")
    with open(made) as f:
        assert f.read() == "x = 1\n"
    assert not os.path.exists(os.path.expanduser("~/Documents/model.py.__never__"))


def test_a_place_the_user_named_still_wins(scratch, tmp_path):
    kind, target = state.resolve_write_destination(
        "ws_create_file",
        {
            "path": "notes.txt",
            "destination_path": str(tmp_path / "out" / "notes.txt"),
            "__session_id__": SID,
        },
    )
    assert (kind, target) == ("absolute", os.path.realpath(tmp_path / "out" / "notes.txt"))


def test_the_write_tools_do_not_raise_the_folder_picker(scratch):
    with open(os.path.join(scratch, "a.txt"), "w") as f:
        f.write("old\n")
    for fn, given in (
        (executors._exec_ws_write_file, {"path": "a.txt", "content": "new\n"}),
        (executors._exec_ws_edit_file, {"path": "a.txt", "old_string": "old", "new_string": "new"}),
        (executors._exec_ws_delete_file, {"path": "a.txt"}),
    ):
        out = _call(fn, given)
        assert not out.startswith("[WS_WORKSPACE_PROMPT]"), out
        assert "No workspace connected" not in out


def test_reading_a_relative_path_reads_the_scratch_folder(scratch):
    with open(os.path.join(scratch, "a.txt"), "w") as f:
        f.write("hello scratch\n")
    assert "hello scratch" in _call(executors._exec_ws_read_file, {"path": "a.txt"})


def test_a_command_with_no_workspace_runs_in_the_scratch_folder(scratch):
    out = _call(executors._exec_ws_run_command, {"command": "pwd"})
    assert "No workspace connected" not in out
    assert os.path.realpath(scratch) in out


def test_an_approved_command_with_no_workspace_runs_in_the_scratch_folder(scratch):
    outcome = asyncio.run(_do_command({"command": "pwd", "session_id": SID}))
    assert outcome.ok, outcome.error
    assert os.path.realpath(scratch) in outcome.output


def test_the_prompt_names_the_working_folder_and_how_the_work_leaves_it(scratch):
    text = _build_no_workspace_section(ws_path=None, session_id=SID)
    assert scratch in text
    assert "folder picker" not in text
    assert "offer to open it or to save it where the user wants" in text
    assert "one folder with a name that says what it holds" in text
    assert "\N{EM DASH}" not in text
    assert _build_no_workspace_section(ws_path="/some/ws", session_id=SID) == ""
