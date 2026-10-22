"""The ws_* file tools change files in two roots: the connected workspace and
the calling session's own scratch folder, where the system prompt sends
throwaway files. Reading is allowed anywhere on the machine (the user's rule),
except the credential stores the sandbox denies and other sessions' scratch
folders: a session works only in its own (the user's rule, 2026-10-22).

Regression: the prompt told the model to put probe scripts in the scratch
folder, every ws_* tool rejected that absolute path as outside the workspace,
and the rejection was routed to the folder picker. In bypass mode, which
auto-approves every real approval, the picker still paused the turn and asked
the user to switch roots, once per probe file. Now the scratch folder is
reachable like the workspace, a new file at any other absolute path goes
through the save approval card, and a modify or delete anywhere else is a
plain refusal the model can act on. The picker fires only when no workspace is
connected at all.
"""

import asyncio
import json
import os

import pytest

from server.approval.executors import _do_delete, _do_write
from server.workspace import executors, paths, state
from server.workspace.paths import _ws_path_allowed, get_scratch_dir, own_scratch_dir

SID = "sess-1"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """A connected workspace and a scratch folder, both under tmp_path."""
    monkeypatch.setattr(paths, "DATA_DIR", str(tmp_path / "data"))
    ws = tmp_path / "ws"
    ws.mkdir()
    for target in (state, executors):
        monkeypatch.setattr(target, "get_workspace_path", lambda: str(ws))
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: str(ws))
    monkeypatch.setattr("server.file_state.check_write_allowed", lambda *a, **k: (True, ""))
    return str(ws), get_scratch_dir(SID)


def _call(fn, tool_input: dict) -> str:
    """Call a ws_* tool the way tool_executor does: stamped with its session."""
    return fn({**tool_input, "__session_id__": SID}, [], [])


def _approval(out: str) -> dict:
    assert out.startswith("[WS_APPROVAL]"), out
    return json.loads(out[len("[WS_APPROVAL]") :])


# ── containment ───────────────────────────────────────────────────────────


def test_own_scratch_folder_counts_as_inside(roots, tmp_path):
    ws, scratch = roots
    assert _ws_path_allowed(os.path.join(ws, "a.py"), ws, SID)
    assert _ws_path_allowed(os.path.join(scratch, "probe.py"), ws, SID)
    assert not _ws_path_allowed(str(tmp_path / "elsewhere.py"), ws, SID)
    assert not _ws_path_allowed("/dev/null", ws, SID)


def test_another_sessions_scratch_folder_is_outside(roots):
    ws, scratch = roots
    other = os.path.join(paths.scratch_root(), "other-session", "x.py")
    assert not _ws_path_allowed(other, ws, SID)
    assert _ws_path_allowed(other, ws, "other-session")
    # Without a session no scratch folder is reachable; the workspace still is.
    assert not _ws_path_allowed(os.path.join(scratch, "probe.py"), ws)
    assert _ws_path_allowed(os.path.join(ws, "a.py"), ws)


@pytest.mark.parametrize("bad", ["", None, "..", ".", "_staging", "a/b", "../x", "a\\b"])
def test_an_id_that_names_no_plain_folder_gets_no_scratch(roots, bad):
    """The id rides in tool and approval payloads: one that would reach the
    parent, the staging area or a nested path names no scratch folder."""
    assert own_scratch_dir(bad) is None


def test_staging_area_stays_out_of_reach(roots):
    """Approvals move finished files out of staging; nothing else may touch
    it, so it is not reachable even though it lives under the scratch root."""
    ws, _ = roots
    assert not _ws_path_allowed(os.path.join(paths._staging_root(), "abc", "f.txt"), ws, SID)


# ── ws_create_file: where a new file goes ─────────────────────────────────


def test_create_in_scratch_is_a_workspace_style_write(roots):
    ws, scratch = roots
    target = os.path.join(scratch, "probe.py")
    payload = _approval(_call(executors._exec_ws_create_file, {"path": target, "content": "1"}))
    assert payload["action"] == "create"
    assert payload["path"] == target
    assert payload["session_id"] == SID


def test_create_with_absolute_path_inside_workspace_uses_the_relative_form(roots):
    ws, _ = roots
    payload = _approval(
        executors._exec_ws_create_file(
            {"path": os.path.join(ws, "src", "a.py"), "content": "x"}, [], []
        )
    )
    assert payload["action"] == "create"
    assert payload["path"] == os.path.join("src", "a.py")


def test_create_outside_both_roots_goes_to_the_save_approval(roots, tmp_path):
    dest = tmp_path / "elsewhere" / "notes.txt"
    out = executors._exec_ws_create_file({"path": str(dest), "content": "hello"}, [], [])
    payload = _approval(out)
    assert payload["action"] == "save_to_path"
    assert payload["path"] == os.path.realpath(str(dest))
    with open(payload["staged_path"], "rb") as f:
        assert f.read() == b"hello"


def test_system_paths_are_refused_outright(roots):
    out = executors._exec_ws_create_file({"path": "/dev/null", "content": "x"}, [], [])
    assert out.startswith("Error:")
    assert "[WS_" not in out


# ── read, modify and delete ───────────────────────────────────────────────


def test_read_write_edit_delete_reach_a_scratch_file(roots):
    ws, scratch = roots
    probe = os.path.join(scratch, "probe.py")
    with open(probe, "w") as f:
        f.write("print(1)\n")

    assert "print(1)" in _call(executors._exec_ws_read_file, {"path": probe})
    assert "probe.py" in _call(executors._exec_ws_list_dir, {"path": scratch})
    written = _approval(_call(executors._exec_ws_write_file, {"path": probe, "content": "2\n"}))
    assert (written["action"], written["path"]) == ("write", probe)
    edited = _approval(
        _call(executors._exec_ws_edit_file, {"path": probe, "old_string": "1", "new_string": "3"})
    )
    assert edited["path"] == probe
    deleted = _approval(_call(executors._exec_ws_delete_file, {"path": probe}))
    assert (deleted["action"], deleted["path"]) == ("delete", probe)
    # Each card carries the session, so its Yes re-checks the same folder.
    assert {written["session_id"], edited["session_id"], deleted["session_id"]} == {SID}


def test_another_sessions_scratch_folder_is_refused_for_every_file_tool(roots):
    """A session works only in its own scratch folder: reading, listing,
    creating, changing and deleting a file in another session's folder are
    all plain refusals that name this session's own folder."""
    ws, scratch = roots
    other_dir = get_scratch_dir("other-session")
    other = os.path.join(other_dir, "notes.txt")
    with open(other, "w") as f:
        f.write("theirs\n")
    calls = (
        (executors._exec_ws_read_file, {}),
        (executors._exec_ws_write_file, {"content": "y"}),
        (executors._exec_ws_edit_file, {"old_string": "theirs", "new_string": "y"}),
        (executors._exec_ws_delete_file, {}),
        (executors._exec_ws_create_file, {"content": "y", "path": other + ".new"}),
    )
    for fn, extra in calls:
        out = _call(fn, {"path": other, **extra})
        assert out.startswith("Error:"), (fn.__name__, out)
        assert "another session's scratch folder" in out
        assert "theirs" not in out
    listed = _call(executors._exec_ws_list_dir, {"path": other_dir})
    assert listed.startswith("Error:") and "notes.txt" not in listed
    assert _call(executors._exec_ws_list_dir, {"path": paths.scratch_root()}).startswith("Error:")
    with open(other) as f:
        assert f.read() == "theirs\n"
    assert not os.path.exists(other + ".new")


def test_modify_outside_both_roots_is_a_plain_refusal(roots, tmp_path):
    """Never the folder picker: the workspace is not in question, the path is,
    and the refusal names both roots so the model can correct itself."""
    ws, _ = roots
    other = tmp_path / "other.txt"
    other.write_text("x")
    calls = (
        (executors._exec_ws_write_file, {"content": "y"}),
        (executors._exec_ws_edit_file, {"old_string": "x", "new_string": "y"}),
        (executors._exec_ws_delete_file, {}),
    )
    for fn, extra in calls:
        out = _call(fn, {"path": str(other), **extra})
        assert out.startswith("Error:"), out
        assert "[WS_WORKSPACE_PROMPT]" not in out
        assert ws in out and own_scratch_dir(SID) in out
    assert other.read_text() == "x"


def test_read_and_list_work_anywhere_outside_both_roots(roots, tmp_path):
    """Reading never asks and is allowed anywhere on the machine."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "notes.txt").write_text("hello\n")
    assert "hello" in executors._exec_ws_read_file({"path": str(other / "notes.txt")}, [], [])
    assert "notes.txt" in executors._exec_ws_list_dir({"path": str(other)}, [], [])


def test_read_by_absolute_path_needs_no_workspace(roots, tmp_path, monkeypatch):
    for target in (state, executors):
        monkeypatch.setattr(target, "get_workspace_path", lambda: None)
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "notes.txt").write_text("hello\n")
    assert "hello" in executors._exec_ws_read_file({"path": str(other / "notes.txt")}, [], [])
    assert "notes.txt" in executors._exec_ws_list_dir({"path": str(other)}, [], [])
    assert executors._exec_ws_read_file({"path": "notes.txt"}, [], []) == "No workspace connected."


def test_a_tilde_path_reads_the_home_folder(roots, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "home-note.txt").write_text("home\n")
    assert "home" in executors._exec_ws_read_file({"path": "~/home-note.txt"}, [], [])


def test_credential_stores_and_system_paths_are_never_read(roots, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("secret")
    out = executors._exec_ws_read_file({"path": str(ssh / "id_rsa")}, [], [])
    assert out.startswith("Error:") and "secret" not in out
    out = executors._exec_ws_list_dir({"path": str(ssh)}, [], [])
    assert out.startswith("Error:") and "id_rsa" not in out
    assert executors._exec_ws_read_file({"path": "/dev/null"}, [], []).startswith("Error:")


def test_folder_picker_only_when_no_workspace_is_connected(roots, monkeypatch):
    for target in (state, executors):
        monkeypatch.setattr(target, "get_workspace_path", lambda: None)
    out = executors._exec_ws_write_file({"path": "a.txt", "content": "x"}, [], [])
    assert out.startswith("[WS_WORKSPACE_PROMPT]")


# ── the approvals that execute those calls ────────────────────────────────


def test_approved_write_and_delete_land_in_scratch(roots):
    _, scratch = roots
    probe = os.path.join(scratch, "probe.txt")
    outcome = asyncio.run(_do_write({"path": probe, "content": "hello", "session_id": SID}))
    assert outcome.ok, outcome.error
    with open(probe) as f:
        assert f.read() == "hello"
    outcome = asyncio.run(_do_delete({"path": probe, "session_id": SID}))
    assert outcome.ok, outcome.error
    assert not os.path.exists(probe)


def test_an_approved_card_reaches_only_its_own_sessions_scratch(roots):
    """The Yes re-checks the path against the session the card carries: a
    card from another session, or one with no session, cannot write here."""
    _, scratch = roots
    probe = os.path.join(scratch, "probe.txt")
    for payload in ({"session_id": "other-session"}, {}):
        outcome = asyncio.run(_do_write({"path": probe, "content": "x", **payload}))
        assert not outcome.ok
    assert not os.path.exists(probe)


def test_approved_write_outside_both_roots_is_refused(roots, tmp_path):
    other = tmp_path / "other.txt"
    outcome = asyncio.run(_do_write({"path": str(other), "content": "x"}))
    assert not outcome.ok
    assert not other.exists()
