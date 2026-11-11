"""Blocking waits after review: the claim check reads task_wait, a user
message ends a wait early, a wait that is not child-agent waiting stops at
the run's time limit, and the workflow launch wiring (Bypass workspaces,
live counters, the session model) holds at the call site.

Behaviour contracts: each test checks how two pieces of data relate.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from types import SimpleNamespace

import pytest

from server.goals.call_results import report_texts, turn_calls

NODE = shutil.which("node") or ""
HARNESS_OK = os.path.exists(NODE) and os.path.exists(
    os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "server", "workflows", "harness", "harness.mjs"
    )
)
needs_node = pytest.mark.skipif(not HARNESS_OK, reason="node/harness missing")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    from server.infrastructure import sessions
    from server.tasks import registry, wait

    storage = tmp_path / "storage"
    os.makedirs(storage, exist_ok=True)
    monkeypatch.setattr(sessions, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(sessions, "DB_PATH", str(storage / "sessions.db"))
    monkeypatch.setattr(registry, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(registry, "DB_PATH", str(storage / "sessions.db"))
    monkeypatch.setattr(wait, "POLL_INTERVAL_SECONDS", 0.01)
    from server.workflows import manager

    manager._live.clear()
    manager._task_ids.clear()
    yield
    manager._live.clear()
    manager._task_ids.clear()


def _rows(*calls):
    """A turn's rows: each (name, input, result) is one tool_use and its
    tool_result."""
    rows = []
    for n, (name, given, result) in enumerate(calls):
        rows.append(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"c{n}", "name": name, "input": given}],
            }
        )
        rows.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"c{n}", "content": result}],
            }
        )
    return rows


def _started(task_id, command):
    return (
        "ws_run_command",
        {"command": command},
        f"[Background Task Started] task_id={task_id}\nCommand: {command}",
    )


def _waited(*items):
    return ("task_wait", {"task_ids": [i["task_id"] for i in items]}, json.dumps({"items": items}))


# The claim check reads task_wait.


def test_a_task_wait_settles_every_background_command_it_names():
    rows = _rows(
        _started("t1", "pytest tests/"),
        _started("t2", "npm test"),
        _waited(
            {
                "task_id": "t1",
                "kind": "shell",
                "status": "completed",
                "exit_code": 0,
                "finished": True,
            },
            {
                "task_id": "t2",
                "kind": "shell",
                "status": "failed",
                "exit_code": 1,
                "finished": True,
            },
        ),
    )
    by_command = {c.input.get("command"): c for c in turn_calls(rows) if c.name == "ws_run_command"}
    assert by_command["pytest tests/"].ok and not by_command["pytest tests/"].pending
    assert not by_command["npm test"].ok and not by_command["npm test"].pending


def test_a_task_wait_that_timed_out_leaves_the_command_pending():
    rows = _rows(
        _started("t1", "pytest tests/"),
        _waited({"task_id": "t1", "kind": "shell", "status": "running", "finished": False}),
    )
    (call,) = [c for c in turn_calls(rows) if c.name == "ws_run_command"]
    assert call.pending and not call.ok


def test_an_agent_report_in_a_task_wait_counts_as_a_report():
    report = "Found 3 call sites of parse_config in server/."
    rows = _rows(
        ("spawn_agent", {"task": "look", "detach": True}, '{"task_id": "a1"}'),
        _waited(
            {
                "task_id": "a1",
                "kind": "agent",
                "status": "completed",
                "finished": True,
                "result_tail": report,
            },
            {"task_id": "t9", "kind": "shell", "status": "completed", "finished": True},
        ),
    )
    texts = report_texts(turn_calls(rows), rows)
    assert any(report in t for t in texts)


# A user message ends a wait in the chat turn.


def test_a_user_message_ends_the_chat_turns_wait_early():
    from server.chat.engine import midturn_inbox
    from server.tasks import registry, wait

    task_id = registry.create_task("shell", session_id="s-in", title="long build")
    bounds = wait.WaitBounds(inbox_session="s-in")

    async def go():
        wait.wait_bounds.set(bounds)
        waiting = asyncio.ensure_future(
            wait.execute_task_wait({"task_ids": [task_id], "timeout_seconds": 60}, "s-in")
        )
        await asyncio.sleep(0.05)
        assert not waiting.done()
        midturn_inbox.push("s-in", "stop, just give me what you have")
        return await asyncio.wait_for(waiting, 5)

    try:
        out = json.loads(asyncio.run(go()))
    finally:
        midturn_inbox.clear("s-in")
    assert out["interrupted"] == "user_message" and out["timed_out"] is False
    assert out["items"][0]["status"] == "running"


def test_a_turn_that_does_not_read_the_inbox_keeps_waiting():
    from server.chat.engine import midturn_inbox
    from server.tasks import registry, wait

    task_id = registry.create_task("shell", session_id="s-sub", title="long build")
    midturn_inbox.push("s-sub", "a message for the chat turn")
    try:
        # A subagent shares the session id but has no inbox_session.
        out = json.loads(
            asyncio.run(
                wait.execute_task_wait({"task_ids": [task_id], "timeout_seconds": 0.1}, "s-sub")
            )
        )
    finally:
        midturn_inbox.clear("s-sub")
    assert out["timed_out"] is True and "interrupted" not in out


# A wait that is not child-agent waiting stops at the run's time limit.


def test_the_timeout_is_cut_to_the_time_the_run_has_left():
    from server.tasks.wait import WaitBounds, bounded_timeout

    soon = time.monotonic() + 5
    unscoped = WaitBounds(deadline=soon)  # cron or headless: no ToolScope
    scoped = WaitBounds(deadline=soon, scoped=True)
    assert bounded_timeout(600, True, unscoped) <= 5
    assert bounded_timeout(600, False, scoped) <= 5
    # An agent waiting on agents: that time moves its deadline instead.
    assert bounded_timeout(600, True, scoped) == 600
    assert bounded_timeout(600, True, WaitBounds()) == 600
    assert bounded_timeout(600, False, WaitBounds(deadline=time.monotonic() - 1)) == 0


def test_a_headless_wait_returns_at_the_deadline():
    from server.tasks import registry, wait

    task_id = registry.create_task("agent", session_id="s-cron", title="worker")

    async def go():
        wait.wait_bounds.set(wait.WaitBounds(deadline=time.monotonic() + 0.1))
        return await asyncio.wait_for(
            wait.execute_task_wait({"task_ids": [task_id], "timeout_seconds": 1800}, "s-cron"), 5
        )

    out = json.loads(asyncio.run(go()))
    assert out["timed_out"] is True


def test_waits_on_agents_reads_the_task_kinds():
    from server.tasks import registry
    from server.tasks.wait import waits_on_agents

    agent = registry.create_task("agent", session_id="s1", title="a")
    shell = registry.create_task("shell", session_id="s1", title="b")
    assert waits_on_agents("task_wait", {"task_ids": [agent]})
    assert not waits_on_agents("task_wait", {"task_ids": [agent, shell]})
    assert not waits_on_agents("task_wait", {"task_ids": ["missing"]})
    assert waits_on_agents("workflow_wait", {"run_ids": ["r1"]})


# Wiring at the call site.


def _launch(monkeypatch, mode, folder):
    from server.workflows import manager, tools

    started = {}
    monkeypatch.setattr("server.security.permissions.load_permissions", lambda: {"mode": mode})
    monkeypatch.setattr(tools, "is_session_granted", lambda *a: True)
    monkeypatch.setattr(tools, "parse_workflow", lambda s: {"name": "a", "phases": []})
    monkeypatch.setattr(manager, "start_run", lambda src, **k: started.update(k) or "rid")
    msg, _side = asyncio.run(
        tools.execute_workflow_run(
            {"script": "export const meta = {name:'a'}", "workspace_path": folder},
            "s1",
            "global.anthropic.claude-sonnet-5",
            None,
        )
    )
    return msg, started


@pytest.mark.parametrize("mode", ["bypassPermissions", "default"])
def test_workflow_run_pins_an_outside_folder_only_in_bypass(tmp_path, monkeypatch, mode):
    ws = tmp_path / "ws"
    other = tmp_path / "elsewhere"
    ws.mkdir()
    other.mkdir()
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(ws))
    msg, started = _launch(monkeypatch, mode, str(other))
    if mode == "bypassPermissions":
        assert started.get("workspace_path") == os.path.realpath(str(other)), msg
    else:
        assert started == {} and "outside the connected workspace" in msg


@needs_node
def test_get_run_shows_the_live_counters_of_a_real_run():
    from server.workflows import manager
    from server.workflows.runtime import WorkflowRun

    rid = "run-live-real"
    with manager._conn() as conn:
        manager._ensure_table(conn)
        conn.execute(
            "INSERT INTO workflow_runs (run_id, name, session_id, status, started_at) "
            "VALUES (?,?,?,?,?)",
            (rid, "n", "s1", "running", manager._now()),
        )
    seen = {}

    async def agent(prompt, opts):
        if prompt == "second":
            seen.update(manager.get_run(rid))
        return {"text": prompt, "output": None, "usage": {"output_tokens": 11}}

    src = (
        "export const meta = { name: 'x', description: 'y', phases: ['a'] }\n"
        "await agent('first')\n"
        "await agent('second')\n"
        "return 'ok'\n"
    )
    run = WorkflowRun(rid, src, session_id="s1", agent_runner=agent)
    manager._live[rid] = run
    out = asyncio.run(run.run())
    assert out["status"] == "done", out
    # Read mid-run: the first agent had finished, the row still held zeros.
    assert seen["live"] is True
    assert seen["agents_spawned"] >= 1 and seen["tokens_out"] >= 11


@pytest.mark.parametrize(
    "session_model",
    ["global.anthropic.claude-sonnet-5", "openai.gpt-6-1-sol"],
)
def test_a_blocking_spawn_runs_on_the_chat_model(monkeypatch, session_model):
    from server.agent_tools import spawn

    seen = {}

    async def fake_run_agent(task, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            agent_id="a1",
            agent_type="general",
            status="completed",
            stop_reason="completed",
            turns_used=1,
            tools_called=[],
            usage={},
            output="done",
        )

    monkeypatch.setattr("server.agents.runtime.run_agent", fake_run_agent)
    monkeypatch.setattr(spawn, "_persist_agent_result", lambda *a, **k: None)
    out = json.loads(
        asyncio.run(spawn.execute_spawn_agent({"task": "look"}, "s1", model_id=session_model))
    )
    assert out["status"] == "completed"
    assert seen["model_id_override"] == session_model
