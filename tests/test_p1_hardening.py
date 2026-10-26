"""P1 hardening from the write-route audit.

- run_python runs an isolated script (private dir + python -P), so a planted
  module cannot shadow the standard library.
- office_script and shell hooks are confined to the workspace.
- a trusted skill auto-runs only its own scripts, matched by path token.
- skill_manage changes only skills the assistant created.
- an MCP tool that needs approval is never run unattended.
- a session's agent journals are removed when the session is deleted.
"""

from __future__ import annotations

import asyncio
import os
import types

import pytest


def _fake_run(captured, *, returncode=0, stdout="ok", stderr=""):
    def fake(command, **kw):
        captured["command"] = command
        captured.update(kw)
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return fake


def test_run_python_runs_an_isolated_script(monkeypatch):
    import server.executors.code as code

    captured: dict = {}
    monkeypatch.setattr(code, "run_sandboxed", _fake_run(captured))
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    monkeypatch.setattr("server.workspace.own_scratch_dir", lambda _s: None)

    ok, _out = code.do_run_python({"code": "print(1)"})
    assert ok
    command = captured["command"]
    assert " -P " in command  # the script dir is never prepended to sys.path
    assert "whisper_run_" in command and "script.py" in command
    assert "mkstemp" not in command
    # The private script directory is cleaned up afterwards.
    import re

    path = re.search(r"(\S*whisper_run_\S*script\.py)", command).group(1)
    assert not os.path.exists(os.path.dirname(path))


def test_office_script_is_confined_to_the_work_dir(monkeypatch):
    import server.documents.script as sc

    captured: dict = {}
    monkeypatch.setattr(
        sc, "run_sandboxed", _fake_run(captured, returncode=1, stdout="", stderr="x")
    )
    sc.run_office_script("print(1)", "docx")
    assert captured.get("write_mode") == "workspace"


def test_a_shell_hook_is_confined_to_the_workspace(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("server.sandbox.run_sandboxed", _fake_run(captured, stdout=""))
    from server.hooks.engine import _run_shell_hook
    from server.hooks.schema import HookDef

    _run_shell_hook(
        HookDef(event="PreToolUse", command="true"), {"event": "PreToolUse"}, "/ws", "/ws"
    )
    assert captured.get("write_mode") == "workspace"
    assert captured.get("write_root") == "/ws"


def test_trusted_skill_matches_a_path_token_not_a_substring(monkeypatch, tmp_path):
    import server.skills as skills

    sd = tmp_path / "agenthub"
    (sd / "scripts").mkdir(parents=True)
    script = sd / "scripts" / "x.py"
    script.write_text("")
    monkeypatch.setattr(skills, "SKILLS", {"agenthub": {"skill_dir": str(sd)}})
    monkeypatch.setattr(skills, "load_skills_config", lambda: {"trusted": ["agenthub"]})

    # The skill's own script, run by path: auto-approvable.
    assert skills.command_runs_trusted_skill(f"python3 {script}")
    # A different program, with the trusted dir only inside an unrelated flag
    # value: NOT a path token, so it is not auto-approved (it asks instead).
    assert not skills.command_runs_trusted_skill(f"python3 /tmp/evil.py --data={sd}/scripts/x")
    assert not skills.command_runs_trusted_skill("rm -rf /")


def test_skill_manage_changes_only_agent_created_skills(monkeypatch, tmp_path):
    import server.agent_tools.skill_manage as sm

    sd = tmp_path / "s"
    sd.mkdir()
    md = sd / "SKILL.md"
    monkeypatch.setattr(sm, "_skill_dir_for", lambda _n: str(sd))
    monkeypatch.setattr(sm, "_enabled", lambda: True)

    md.write_text("---\nname: s\n---\nbody\n")
    assert sm._require_agent_created("s") is not None  # user skill: refused
    out = sm.execute_skill_manage({"action": "patch", "name": "s", "description": "d"})
    assert "Settings" in out  # a mutation is refused before any write

    md.write_text("---\nname: s\ncreated_by: agent\n---\nbody\n")
    assert sm._require_agent_created("s") is None  # the assistant's own: allowed


def test_an_mcp_tool_needing_approval_is_refused_unattended():
    from server.approval.executors import _do_mcp_tool_call

    out = asyncio.run(
        _do_mcp_tool_call({"tool_key": "mcp__x__y", "arguments": {}, "__agent__": True})
    )
    assert not out.ok and "unattended" in (out.error or "")


def test_session_delete_removes_the_agent_journals(monkeypatch, tmp_path):
    import server.agents.journal as journal

    monkeypatch.setattr(journal, "storage_root", lambda: str(tmp_path))
    sess = journal._safe("sess1")
    run_dir = os.path.join(journal.agents_root(), sess, "agent1")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "report.md"), "w") as f:
        f.write("done")

    assert os.path.isdir(os.path.join(journal.agents_root(), sess))
    journal.drop_session("sess1")
    assert not os.path.exists(os.path.join(journal.agents_root(), sess))
    # A session with no journals is a no-op, not an error.
    journal.drop_session("never-ran")
