"""Part 3: promote_agent_type — re-validate a session's ephemeral definition
and write it out as a persistent .whisper/agents/<name>.md custom type; Part
1's loader must read it back into an equivalent AgentConfig (round-trip)."""

import json
import os

from server.agent_tools.promote import execute_promote_agent_type
from server.agents import custom_config as cc
from server.agents.custom_config import load_custom_agent_types, register_ephemeral_type


def test_promote_writes_file_project_scope_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr("server.infrastructure.paths.config_dir", lambda: str(tmp_path / "home"))
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: str(tmp_path / "project"))

    session_id = "sess-promote-1"
    register_ephemeral_type(
        session_id,
        {
            "name": "log_scanner",
            "description": "Scans logs for errors",
            "tools": ["ws_read_file", "ws_grep"],
            "read_only": True,
            "max_turns": 15,
            "system_prompt": "Scan logs for ERROR/WARN lines and summarize them.",
        },
    )

    out = json.loads(execute_promote_agent_type({"name": "log_scanner"}, session_id))

    assert out["saved"] is True
    assert out["name"] == "log_scanner"
    assert out["read_only"] is True
    assert out["tools"] == ["ws_grep", "ws_read_file"]
    path = out["path"]
    assert os.path.isfile(path)
    assert path == os.path.join(str(tmp_path / "project"), ".whisper", "agents", "log_scanner.md")

    types = load_custom_agent_types(workspace_path=str(tmp_path / "project"))
    reloaded = types["log_scanner"]
    assert reloaded.read_only is True
    assert reloaded.allowed_tools == frozenset({"ws_read_file", "ws_grep"})
    assert reloaded.max_turns == 15
    assert "Scan logs" in reloaded.system_prompt


def test_promote_unknown_name_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr("server.infrastructure.paths.config_dir", lambda: str(tmp_path / "home"))
    out = json.loads(execute_promote_agent_type({"name": "does_not_exist"}, "sess-empty"))
    assert "error" in out


def test_promote_missing_name_returns_error():
    out = json.loads(execute_promote_agent_type({}, "sess-empty"))
    assert "error" in out


def test_promote_user_scope(tmp_path, monkeypatch):
    monkeypatch.setattr("server.infrastructure.paths.config_dir", lambda: str(tmp_path / "home"))
    session_id = "sess-promote-2"
    register_ephemeral_type(
        session_id,
        {"name": "user_scoped_bot", "description": "user scope test", "read_only": True},
    )

    out = json.loads(
        execute_promote_agent_type({"name": "user_scoped_bot", "scope": "user"}, session_id)
    )

    assert out["saved"] is True
    assert str(tmp_path / "home") in out["path"]
    types = load_custom_agent_types(workspace_path=str(tmp_path / "nonexistent_project"))
    assert "user_scoped_bot" in types


def test_promote_project_scope_without_workspace_errors(tmp_path, monkeypatch):
    monkeypatch.setattr("server.infrastructure.paths.config_dir", lambda: str(tmp_path / "home"))
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    session_id = "sess-promote-3"
    register_ephemeral_type(
        session_id, {"name": "no_workspace_bot", "description": "d", "read_only": True}
    )

    out = json.loads(execute_promote_agent_type({"name": "no_workspace_bot"}, session_id))

    assert "error" in out
    assert "workspace" in out["error"].lower()


def test_promote_re_strips_write_tool_even_if_stored_def_is_tampered(tmp_path, monkeypatch):
    """promote_ephemeral_type must not blindly trust the stored AgentConfig —
    it rebuilds from the RAW recorded fields every time, so even a stored def
    that (somehow) claims read_only=True alongside a write tool gets
    re-sanitized independently at promote time, not just at spawn time."""
    monkeypatch.setattr("server.infrastructure.paths.config_dir", lambda: str(tmp_path / "home"))
    session_id = "sess-tamper"
    register_ephemeral_type(
        session_id,
        {"name": "tampered", "description": "x", "read_only": True, "tools": ["ws_write_file"]},
    )
    stored = cc.get_session_ephemeral_def(session_id, "tampered")
    # Simulate a bypassed spawn-time sanitizer by directly overwriting the
    # in-memory record with one that (incorrectly) kept the write tool.
    corrupted = cc.EphemeralAgentDef(
        name=stored.name,
        description=stored.description,
        tools=("ws_write_file", "ws_read_file"),
        read_only=True,
        model=stored.model,
        max_turns=stored.max_turns,
        system_prompt=stored.system_prompt,
    )
    with cc._lock:
        cc._SESSION_EPHEMERAL[session_id]["tampered"] = corrupted

    out = json.loads(execute_promote_agent_type({"name": "tampered", "scope": "user"}, session_id))

    assert out["saved"] is True
    assert out["tools"] == ["ws_read_file"]  # ws_write_file stripped again at promote time


def test_promote_rejects_unsafe_name(tmp_path, monkeypatch):
    monkeypatch.setattr("server.infrastructure.paths.config_dir", lambda: str(tmp_path / "home"))
    session_id = "sess-unsafe"
    # Bypass register_ephemeral_type's own sanitizing to plant an unsafe raw
    # name directly, exercising promote_ephemeral_type's own guard.
    with cc._lock:
        cc._SESSION_EPHEMERAL.setdefault(session_id, {})["../evil"] = cc.EphemeralAgentDef(
            name="../evil",
            description="d",
            tools=None,
            read_only=True,
            model=None,
            max_turns=None,
            system_prompt=None,
        )

    try:
        cc.promote_ephemeral_type(session_id, "../evil", scope="user")
        raised = False
    except ValueError:
        raised = True
    assert raised
