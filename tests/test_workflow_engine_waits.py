"""Workflow engine: incomplete agents, blocking waits, live counters, script
status reads, the budget floor, Bypass workspaces, the sleep cap, the
ultracode sizing directive, and the default agent model.

Behaviour contracts: each test checks how two pieces of data relate (what the
adapter gets from run_agent and what the script reads, what the ledger holds
and what workflow_status shows), never a frozen catalog.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from types import SimpleNamespace

import pytest

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
    from server.tasks import registry

    storage = tmp_path / "storage"
    os.makedirs(storage, exist_ok=True)
    monkeypatch.setattr(sessions, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(sessions, "DB_PATH", str(storage / "sessions.db"))
    monkeypatch.setattr(registry, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(registry, "DB_PATH", str(storage / "sessions.db"))
    from server.workflows import manager

    manager._live.clear()
    manager._task_ids.clear()
    yield
    manager._live.clear()
    manager._task_ids.clear()


def _agent_result(**kw):
    base = {
        "agent_id": "a1",
        "agent_type": "general",
        "output": "partial notes",
        "status": "completed",
        "stop_reason": "completed",
        "structured_output": None,
        "usage": {"output_tokens": 7},
        "stopped_early": False,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _adapter_call(monkeypatch, result, default_model_id="session-model", opts=None):
    seen: dict = {}

    async def fake_run_agent(prompt, **kwargs):
        seen.update(kwargs)
        return result

    monkeypatch.setattr("server.agents.runtime.run_agent", fake_run_agent)
    from server.workflows.agent_adapter import run_workflow_agent

    out = asyncio.run(
        run_workflow_agent(
            "do it",
            opts or {},
            session_id="s1",
            default_model_id=default_model_id,
            effort_label=None,
            workspace_path=None,
            run_id="r1",
            depth=1,
        )
    )
    return out, seen


# 1. An agent a limit stopped reports "incomplete" to the script.


def test_limit_stop_reaches_the_script_as_incomplete(monkeypatch):
    res = _agent_result(stopped_early=True, stop_reason="deadline")
    out, _ = _adapter_call(monkeypatch, res)
    assert out["status"] == "incomplete"
    assert out["stop_reason"] == res.stop_reason
    assert out["stopped_early"] is True


def test_clean_finish_and_failure_keep_their_status(monkeypatch):
    done, _ = _adapter_call(monkeypatch, _agent_result())
    assert done["status"] == "completed" and done["stopped_early"] is False
    failed, _ = _adapter_call(monkeypatch, _agent_result(status="failed", stop_reason="error"))
    assert failed["status"] == "failed"


@needs_node
def test_incomplete_agents_are_counted_journaled_and_never_replayed(tmp_path):
    from server.workflows.journal import load_resume_cache, read_journal
    from server.workflows.runtime import WorkflowRun

    async def fake_agent(prompt, opts):
        stopped = prompt == "big"
        return {
            "text": prompt,
            "output": None,
            "usage": {"output_tokens": 1},
            "status": "incomplete" if stopped else "completed",
            "stop_reason": "turn_limit" if stopped else "completed",
        }

    src = (
        "export const meta = { name: 'x', description: 'y', phases: ['a'] }\n"
        "const r = await parallel([() => agent('big'), () => agent('small')])\n"
        "return r.map(x => x.status)\n"
    )
    run = WorkflowRun("run-inc", src, agent_runner=fake_agent)
    out = asyncio.run(run.run())
    assert sorted(out["result"]) == ["completed", "incomplete"]
    assert out["agents_incomplete"] == 1
    calls = [e for e in read_journal("run-inc") if e.get("type") == "agent_call"]
    stopped = [e for e in calls if e["status"] == "incomplete"]
    assert [e["stop_reason"] for e in stopped] == ["turn_limit"]
    # A resume replays the finished agent only; the partial one runs again.
    cache = load_resume_cache("run-inc")
    assert sum(len(v) for v in cache.values()) == len(calls) - len(stopped)


def test_status_view_and_result_text_name_incomplete_agents():
    from server.workflows.manager import _task_result_text
    from server.workflows.tools import _status_view

    journal = [
        {"type": "agent_call", "seq": 0, "status": "completed", "label": "ok"},
        {
            "type": "agent_call",
            "seq": 1,
            "status": "incomplete",
            "label": "cut",
            "stop_reason": "deadline",
        },
    ]
    view = _status_view({"run_id": "r", "status": "done", "journal": journal})
    assert [a["label"] for a in view["incomplete_agents"]] == ["cut"]
    text = _task_result_text("done", {"agents_spawned": 2, "agents_incomplete": 1})
    assert "incomplete" in text and "partial" in text


# 4. Live counters.


def test_get_run_reads_the_live_ledger_for_a_running_run():
    from server.workflows import manager

    rid = "live-run"
    with manager._conn() as conn:
        manager._ensure_table(conn)
        conn.execute(
            "INSERT INTO workflow_runs (run_id, name, session_id, status, started_at) "
            "VALUES (?,?,?,?,?)",
            (rid, "n", "s1", "running", manager._now()),
        )

    class _Live:
        def live_counters(self):
            return {"agents_spawned": 3, "tokens_out": 40, "cost_usd": 1.25}

    manager._live[rid] = _Live()
    row = manager.get_run(rid)
    assert row["live"] is True
    assert (row["agents_spawned"], row["tokens_out"], row["cost_usd"]) == (3, 40, 1.25)
    manager._live.pop(rid)
    row = manager.get_run(rid)
    # Without a live run the row's own (end-of-run) counters stand.
    assert row["live"] is False and row["agents_spawned"] == 0


# 3. Tool scripts may read run status.


def test_run_status_tools_are_read_only_executors():
    from server.executors import EXECUTORS, is_read_only
    from server.tasks import tools as _task_tools  # noqa: F401 (registers task_*)

    for name in (
        "workflow_status",
        "workflow_list",
        "task_status",
        "task_output",
        "session_search",
    ):
        assert is_read_only(name), name
        assert name in EXECUTORS, name


@needs_node
def test_a_tool_script_can_call_workflow_status():
    from server.workflows.runtime import WorkflowRun

    async def no_agent(prompt, opts):
        raise AssertionError("no agents here")

    src = (
        "export const meta = { name: 'x', description: 'y', phases: ['a'] }\n"
        "const s = await tools.workflow_status({})\n"
        "const l = await tools.workflow_list({})\n"
        "return { s, l }\n"
    )
    run = WorkflowRun("run-ptc", src, session_id="s-ptc", agent_runner=no_agent)
    out = asyncio.run(run.run())
    assert out["status"] == "done", out
    assert "No workflow runs" in out["result"]["s"]
    assert "saved_workflows" in json.loads(out["result"]["l"])


# 6. Budget floor.


def test_budget_below_the_floor_is_refused_before_launch(monkeypatch):
    from server.workflows import manager, tools
    from server.workflows.runtime import MIN_WORKFLOW_BUDGET_TOKENS

    started = []
    monkeypatch.setattr(manager, "start_run", lambda *a, **k: started.append(1) or "x")
    msg, side = asyncio.run(
        tools.execute_workflow_run(
            {
                "script": "export const meta = {name:'a'}",
                "budget_tokens": MIN_WORKFLOW_BUDGET_TOKENS - 1,
            },
            "s1",
            "m",
            None,
        )
    )
    assert str(MIN_WORKFLOW_BUDGET_TOKENS) in msg and "Not started" in msg
    assert side == [] and started == []
    assert tools.budget_error(MIN_WORKFLOW_BUDGET_TOKENS) == ""
    assert tools.budget_error(None) == ""


# 5. Bypass pins a run outside the workspace.


def test_bypass_accepts_a_folder_outside_the_workspace(tmp_path, monkeypatch):
    from server.security import folder_grants as fg
    from server.workflows import manager

    monkeypatch.setattr(fg, "GRANTS_PATH", str(tmp_path / "grants.json"))
    ws = tmp_path / "ws"
    other = tmp_path / "elsewhere"
    ws.mkdir()
    other.mkdir()
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(ws))
    path, err = manager.resolve_workflow_workspace(str(other))
    assert path is None and "outside the connected workspace" in err
    path, err = manager.resolve_workflow_workspace(str(other), bypass=True)
    assert err == "" and path == os.path.realpath(str(other))
    # Bypass never invents a folder.
    path, err = manager.resolve_workflow_workspace(str(tmp_path / "missing"), bypass=True)
    assert path is None and "does not exist" in err


def test_is_bypass_follows_the_workflow_mode(monkeypatch):
    from server.workflows import launch_policy

    monkeypatch.setattr(
        "server.security.permissions.load_permissions", lambda: {"mode": "bypassPermissions"}
    )
    assert launch_policy.is_bypass() is True
    monkeypatch.setattr(
        "server.security.permissions.load_permissions",
        lambda: {"mode": "bypassPermissions", "category_modes": {"workflow": "default"}},
    )
    assert launch_policy.is_bypass() is False


# 2. Blocking waits.


def test_wait_all_any_timeout_and_unknown_ids(monkeypatch):
    from server.tasks import wait

    monkeypatch.setattr(wait, "POLL_INTERVAL_SECONDS", 0.01)
    state = {"a": "running", "b": "running"}
    ticks = {"n": 0}

    def view(i):
        ticks["n"] += 1
        if ticks["n"] > 4:
            state["a"] = "completed"
        if i not in state:
            return {"id": i, "status": "not_found", "finished": True}
        return {"id": i, "status": state[i], "finished": state[i] != "running"}

    any_done = asyncio.run(wait.wait_for(["a", "b"], view, mode="any", timeout=2))
    assert any_done["timed_out"] is False
    assert [v["id"] for v in any_done["items"] if v["finished"]] == ["a"]
    all_wait = asyncio.run(wait.wait_for(["a", "b"], view, mode="all", timeout=0.05))
    assert all_wait["timed_out"] is True and "hint" in all_wait
    unknown = asyncio.run(wait.wait_for(["zz"], view, timeout=5))
    assert unknown["timed_out"] is False and unknown["items"][0]["status"] == "not_found"
    assert wait.wait_seconds(10**6) == wait.MAX_WAIT_SECONDS
    assert wait.wait_seconds(None) == wait.DEFAULT_WAIT_SECONDS


def test_task_wait_returns_when_the_task_finishes(monkeypatch):
    from server.tasks import registry, wait

    monkeypatch.setattr(wait, "POLL_INTERVAL_SECONDS", 0.01)
    tid = registry.create_task("shell", session_id="s1", title="t")

    async def scenario():
        async def finish_later():
            await asyncio.sleep(0.05)
            registry.finish_task(tid, status="completed", exit_code=0, result_text="all good")

        asyncio.get_running_loop().create_task(finish_later())
        return await wait.execute_task_wait({"task_ids": [tid], "timeout_seconds": 5}, "s1")

    out = json.loads(asyncio.run(scenario()))
    assert out["timed_out"] is False
    item = out["items"][0]
    assert item["status"] == "completed" and item["result_tail"] == "all good"
    # Another session's task shows its status, never its result.
    other = json.loads(
        asyncio.run(wait.execute_task_wait({"task_ids": [tid], "timeout_seconds": 1}, "s2"))
    )
    assert "result_tail" not in other["items"][0]


def test_workflow_wait_reports_the_finished_run(monkeypatch):
    from server.tasks import wait
    from server.workflows import manager, tools

    monkeypatch.setattr(wait, "POLL_INTERVAL_SECONDS", 0.01)
    with manager._conn() as conn:
        manager._ensure_table(conn)
        conn.execute(
            "INSERT INTO workflow_runs (run_id, name, session_id, status, agents_spawned, "
            "started_at) VALUES (?,?,?,?,?,?)",
            ("wf1", "n", "s1", "done", 2, manager._now()),
        )
    out = json.loads(asyncio.run(tools.execute_workflow_wait({"run_ids": ["wf1"]}, "s1")))
    assert out["timed_out"] is False
    assert out["items"][0]["status"] == "done" and out["items"][0]["agents_spawned"] == 2


def test_waits_count_as_child_agent_waiting_and_stay_in_the_catalog():
    from server.agents.tool_access import WAITS_ON_AGENTS
    from server.chat.tool_partition import CORE_TOOLS
    from server.chat.tool_pool import assemble_full_catalog

    assert {"task_wait", "workflow_wait"} <= WAITS_ON_AGENTS
    plain = {t["name"] for t in assemble_full_catalog()}
    ultra = {t["name"] for t in assemble_full_catalog(ultracode=True)}
    assert "task_wait" in plain and "task_wait" in ultra
    assert "workflow_wait" in ultra
    assert {"task_wait", "workflow_wait"} <= CORE_TOOLS


@pytest.mark.parametrize("name", ["task_wait", "workflow_wait"])
def test_wait_tools_are_routed(monkeypatch, name):
    from server import tool_router

    async def fake(tool_input, session_id):
        return f"waited:{session_id}"

    target = (
        "server.tasks.wait.execute_task_wait"
        if name == "task_wait"
        else "server.workflows.tools.execute_workflow_wait"
    )
    monkeypatch.setattr(target, fake)
    out, _ = asyncio.run(
        tool_router.route_tool(
            name,
            {"task_ids": ["x"], "run_ids": ["x"]},
            loop=None,
            executor=None,
            transcript="",
            attachments={},
            session_id="s9",
            model_id="m",
            tool_use_id="tu1",
        )
    )
    assert out == "waited:s9"


def test_hints_teach_waiting_not_polling():
    from server.ask_user import SLEEP_TOOL
    from server.tasks.tools import BACKGROUND_TASK_TOOLS
    from server.workflows.tools import WORKFLOW_TOOLS

    run = next(t for t in WORKFLOW_TOOLS if t["name"] == "workflow_run")
    assert "workflow_wait" in run["description"]
    assert "poll workflow_status" not in run["description"]
    status = next(t for t in BACKGROUND_TASK_TOOLS if t["name"] == "task_status")
    assert "task_wait" in status["description"]
    assert "task_wait" in SLEEP_TOOL["description"]


def test_detached_spawn_hint_says_completion_wakes_you(monkeypatch):
    from server.agent_tools import spawn

    monkeypatch.setattr("server.tasks.agents.start_detached_agent", lambda *a, **k: "t-1")
    out = json.loads(spawn._start_detached_from_tool("task", "general", "", "s1", "m", None))
    assert "task_wait" in out["hint"] and "Poll" not in out["hint"]


# Sleep cap.


def test_sleep_gets_a_note_then_a_cap(monkeypatch):
    from server.chat import loop_guard as lg

    monkeypatch.setattr(lg, "enabled", lambda: True)
    monkeypatch.setattr(lg, "caps", lambda: dict(lg.DEFAULT_CAPS))
    scope = "sleep-scope"
    lg.reset_for_turn(scope)
    cap = lg.DEFAULT_CAPS["sleep"]
    notes = []
    for _ in range(cap):
        assert lg.before_call(scope, "sleep", {"seconds": 5}).allow
        notice, _stub = lg.after_call(scope, "sleep", {"seconds": 5}, "Slept", is_error=False)
        notes.append(notice)
    assert all(n is None for n in notes[: lg.SLEEP_NOTE_FROM - 1])
    assert all(n and "task_wait" in n for n in notes[lg.SLEEP_NOTE_FROM - 1 :])
    refused = lg.before_call(scope, "sleep", {"seconds": 5})
    assert not refused.allow and "workflow_wait" in refused.message
    lg.reset_for_turn(scope)


# 7. The ultracode directive.


def test_directive_states_live_limits_and_sizing(monkeypatch):
    from server.infrastructure import run_limits
    from server.prompts import ultracode

    monkeypatch.setattr(run_limits, "round_limit", lambda: 77)
    monkeypatch.setattr(run_limits, "time_limit_seconds", lambda: 23 * 60.0)
    monkeypatch.setattr("server.workflows.store.list_scripts", lambda: [])
    text = ultracode.build_ultracode_directive()
    assert "77 rounds" in text and "23 minutes" in text
    assert "up to 16 at once" in text and "Never use a fixed count" in text
    assert 'isolation:"worktree"' in text and "checker" in text
    assert "incomplete" in text and "workflow_wait" in text
    assert "Do not set `opts.model` unless the user asked" in text
    assert "\u2014" not in text and "{round_limit}" not in text


# 8. The default agent model is the chat's selected model.


@pytest.mark.parametrize(
    "session_model",
    ["global.anthropic.claude-sonnet-5", "openai.gpt-6-1-sol"],
)
def test_workflow_agent_default_model_is_the_chat_model(monkeypatch, session_model):
    _out, seen = _adapter_call(monkeypatch, _agent_result(), default_model_id=session_model)
    assert seen["model_id_override"] == session_model


@pytest.mark.parametrize(
    "session_model",
    ["global.anthropic.claude-sonnet-5", "openai.gpt-6-1-sol"],
)
def test_spawn_agent_default_model_is_the_chat_model(monkeypatch, session_model):
    from server.agent_tools import spawn

    seen = {}

    def fake_start(task, **kwargs):
        seen.update(kwargs)
        return "t-1"

    monkeypatch.setattr("server.tasks.agents.start_detached_agent", fake_start)
    asyncio.run(
        spawn.execute_spawn_agent({"task": "look", "detach": True}, "s1", model_id=session_model)
    )
    assert seen["model_id"] == session_model


def test_workflow_run_launches_on_the_chat_model(monkeypatch):
    from server.workflows import manager, tools

    seen = {}
    monkeypatch.setattr(tools, "is_session_granted", lambda *a: True)
    monkeypatch.setattr(tools, "parse_workflow", lambda s: {"name": "a", "phases": []})
    monkeypatch.setattr(manager, "start_run", lambda src, **k: seen.update(k) or "rid")
    monkeypatch.setattr(manager, "resolve_workflow_workspace", lambda *a, **k: (None, ""))
    asyncio.run(tools.execute_workflow_run({"script": "x"}, "s1", "openai.gpt-6-1-sol", None))
    assert seen["model_id"] == "openai.gpt-6-1-sol"
