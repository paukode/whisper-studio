"""Bypass shows no approval card (the user's decision of 2026-11-02).

Bypass works like Claude Code's bypass: a delete outside the workspace, a
sandbox escalation, a save outside the workspace, reach into a new folder, and
a ws_* file change anywhere on the machine run with no card. What still asks
or refuses in Bypass: a destructive GitHub action, a tool of an MCP server
marked approval_mode "approve", a category row of the user's that names
another mode, and the rm -rf / hard block. Every other mode keeps its rule.
"""

import asyncio
import json

import pytest

from server.security.permissions import (
    MODE_ACCEPT_EDITS,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_DONT_ASK,
    approval_floor,
    bypass_call,
    resolve_static_decision,
)
from server.security.secret_reads import SECRET_READS_KEY
from server.workspace import executors

NON_BYPASS = (MODE_DEFAULT, MODE_ACCEPT_EDITS, MODE_DONT_ASK)


@pytest.fixture
def perms(monkeypatch):
    """The user's permissions.json, as resolve_static_decision reads it."""
    data = {"mode": MODE_DEFAULT, "rules": [], "category_modes": {}}
    monkeypatch.setattr("server.security.permissions.load_permissions", lambda: data)
    return data


@pytest.fixture
def app_mode(monkeypatch):
    def pin(mode):
        monkeypatch.setattr("server.security.permissions.get_mode", lambda: mode)

    pin(MODE_DEFAULT)
    return pin


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "project"
    ws.mkdir()
    for target in (
        "server.workspace.state.get_workspace_path",
        "server.workspace.get_workspace_path",
    ):
        monkeypatch.setattr(target, lambda: str(ws))
    monkeypatch.setattr(executors, "get_workspace_path", lambda: str(ws))
    monkeypatch.setattr("server.file_state.check_write_allowed", lambda *a, **k: (True, ""))
    return ws


@pytest.fixture
def outside(tmp_path):
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    note = folder / "note.txt"
    note.write_text("old text\n")
    return note


# ── The floors that stay ─────────────────────────────────────────────────────


def test_a_destructive_github_action_still_asks_in_bypass(perms):
    payload = {"action": "github_merge_pr", "number": 7}
    assert approval_floor("github_merge_pr", payload, "github-destructive", MODE_BYPASS)
    assert (
        resolve_static_decision(
            "github_merge_pr",
            payload,
            "github-destructive",
            {"github-destructive": "allow"},
            MODE_BYPASS,
        )
        == "ask"
    )


def test_an_mcp_server_marked_approve_still_asks_in_bypass(perms, monkeypatch):
    from server.mcp import mcp_manager

    monkeypatch.setattr(mcp_manager, "get_tool_approval_tier_for_tool_name", lambda name: "approve")
    payload = {"action": "mcp_tool_call", "tool_key": "srv__drop"}
    assert approval_floor("mcp__srv__drop", payload, "mcp", MODE_BYPASS) == "mcp-approve"
    assert resolve_static_decision("mcp__srv__drop", payload, "mcp", {}, MODE_BYPASS) == "ask"


def test_rm_of_the_filesystem_root_is_still_refused_in_bypass():
    from server.security.command_validator import validate_command

    assert validate_command("rm -rf /", secret_reads=True)


def test_a_category_row_that_names_another_mode_still_asks_in_bypass(perms, tmp_path):
    perms["category_modes"] = {"cli": MODE_DEFAULT, "folder-access": MODE_DEFAULT}
    escalate = {"command": "touch ~/x", "sandbox_permissions": "danger-full-access"}
    assert approval_floor("terminal_run", escalate, "cli", MODE_BYPASS) == "sandbox-escalation"
    assert resolve_static_decision("terminal_run", escalate, "cli", {}, MODE_BYPASS) == "ask"
    folder = {"action": "folder_access", "path": str(tmp_path / "other")}
    assert (
        resolve_static_decision("ws_open_folder", folder, "folder-access", {}, MODE_BYPASS) == "ask"
    )


# ── The cards that are gone in Bypass ────────────────────────────────────────


def test_bypass_lifts_the_rm_escalation_and_visible_terminal_floors(perms, workspace):
    calls = (
        ("ws_run_command", {"command": "rm -rf ~/old"}),
        ("ws_run_command", {"command": "rm -rf $BUILD_DIR"}),
        ("terminal_run", {"command": "touch ~/x", "sandbox_permissions": "danger-full-access"}),
        ("terminal_run", {"command": "npm test", "mode": "visible"}),
    )
    for tool, payload in calls:
        assert approval_floor(tool, payload, "cli", MODE_BYPASS) is None, payload
        assert resolve_static_decision(tool, payload, "cli", {}, MODE_BYPASS) == "allow"
        for mode in NON_BYPASS:
            assert approval_floor(tool, payload, "cli", mode) is not None, (payload, mode)
            assert resolve_static_decision(tool, payload, "cli", {}, mode) in ("ask", "deny")


def test_bypass_allows_an_outside_save_and_a_new_folder(perms, tmp_path):
    save = {"action": "save_to_path", "path": str(tmp_path / "Documents" / "a.txt")}
    folder = {"action": "folder_access", "path": str(tmp_path / "other")}
    assert resolve_static_decision("save_file", save, "save", {}, MODE_BYPASS) == "allow"
    assert (
        resolve_static_decision("ws_open_folder", folder, "folder-access", {}, MODE_BYPASS)
        == "allow"
    )
    assert resolve_static_decision("save_file", save, "save", {}, MODE_DEFAULT) == "ask"


# ── ws_* file tools reach anywhere in Bypass ─────────────────────────────────


def _approval(output: str) -> dict:
    assert output.startswith("[WS_APPROVAL]"), output
    return json.loads(output[len("[WS_APPROVAL]") :])


def _calls(note):
    return (
        (executors._exec_ws_write_file, {"path": str(note), "content": "new\n"}),
        (
            executors._exec_ws_edit_file,
            {"path": str(note), "old_string": "old", "new_string": "new"},
        ),
        (executors._exec_ws_delete_file, {"path": str(note)}),
    )


def test_ws_file_tools_change_files_outside_the_workspace_in_bypass(workspace, outside):
    for fn, tool_input in _calls(outside):
        out = fn({**tool_input, "__session_id__": "s1", SECRET_READS_KEY: True}, [], [])
        assert _approval(out)["path"] == str(outside), fn.__name__


def test_ws_file_tools_refuse_outside_the_workspace_in_other_modes(workspace, outside):
    for fn, tool_input in _calls(outside):
        out = fn({**tool_input, "__session_id__": "s1", SECRET_READS_KEY: False}, [], [])
        assert out.startswith("Error:") and "outside the connected workspace" in out


def test_an_agent_of_a_bypass_session_reaches_outside_too(workspace, outside, app_mode):
    agent = {"path": str(outside), "content": "x", "__agent__": True, SECRET_READS_KEY: False}
    app_mode(MODE_BYPASS)
    assert bypass_call(agent)
    assert _approval(executors._exec_ws_write_file(dict(agent), [], []))
    app_mode(MODE_DEFAULT)
    assert not bypass_call(agent)
    assert executors._exec_ws_write_file(dict(agent), [], []).startswith("Error:")


def test_another_sessions_scratch_folder_stays_out_of_reach_in_bypass(workspace):
    from server.workspace.paths import scratch_root

    other = f"{scratch_root()}/other-session/x.txt"
    out = executors._exec_ws_write_file(
        {"path": other, "content": "x", "__session_id__": "s1", SECRET_READS_KEY: True}, [], []
    )
    assert out.startswith("Error:")


def test_the_write_and_delete_executors_follow_the_mode(workspace, outside, app_mode):
    from server.approval.executors import _do_delete, _do_write

    payload = {"path": str(outside), "content": "new\n", "session_id": "s1"}
    app_mode(MODE_DEFAULT)
    assert asyncio.run(_do_write(dict(payload))).ok is False
    assert outside.read_text() == "old text\n"
    app_mode(MODE_BYPASS)
    assert asyncio.run(_do_write(dict(payload))).ok is True
    assert outside.read_text() == "new\n"

    app_mode(MODE_DEFAULT)
    assert asyncio.run(_do_delete({"path": str(outside), "session_id": "s1"})).ok is False
    assert outside.exists()
    app_mode(MODE_BYPASS)
    assert asyncio.run(_do_delete({"path": str(outside), "session_id": "s1"})).ok is True
    assert not outside.exists()
