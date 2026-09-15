"""Two more guards from the same incident (see test_agent_recursion_guards.py
for the recursion/concurrency half):

1. Budget is checked BEFORE dispatching a new team/spawn_agent, not only
   reactively inside that agent's own round loop — so agent N+1 never starts
   once agents 1..N's combined spend already cleared the cap, instead of
   every sibling spending real tokens just to find out on its own.
2. A finished agent's result is written into the durable task registry
   (server/tasks/registry.py) keyed by its own agent_id, the same store a
   detached agent already lands in — so task_output/task_status can find it
   later instead of it being gone the moment it scrolls out of context.
"""

import asyncio
from types import SimpleNamespace

from server import agent_tools
from server.agent_tools import spawn as spawn_mod
from server.costs import budget as budget_mod
from server.tasks import registry


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, channel, ev):
        self.events.append((channel, ev))


def _fake_result(**overrides):
    base = dict(
        agent_id="a1",
        agent_type="explore",
        status="completed",
        turns_used=1,
        tools_called=[],
        usage={},
        output="done",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _no_budget_cap(monkeypatch):
    monkeypatch.setattr(budget_mod, "check_budget", lambda session_id: None)


def _budget_already_over(monkeypatch):
    exceeded = budget_mod.BudgetExceeded(
        kind="daily",
        limit=600.0,
        current=974.81,
        message="Daily cost $974.81 has reached the limit of $600.00.",
    )
    monkeypatch.setattr(budget_mod, "check_budget", lambda session_id: exceeded)


# ── #1: pre-flight budget check ──────────────────────────────────────────────


def test_team_create_refuses_members_when_budget_already_exceeded(monkeypatch):
    monkeypatch.setattr("server.agents.event_bus.event_bus", _Bus())
    monkeypatch.setattr(spawn_mod, "_active_agent_counts", {})
    _budget_already_over(monkeypatch)

    async def _fail_if_called(task, **kwargs):
        raise AssertionError("run_agent must not be called once the budget is already exceeded")

    monkeypatch.setattr("server.agents.runtime.run_agent", _fail_if_called)

    out_json, _payload = asyncio.run(
        agent_tools.execute_team_create(
            {"team_name": "t", "agents": [{"name": "a", "task": "x"}]}, "model-x", "sess1"
        )
    )
    import json

    out = json.loads(out_json)
    assert "Budget exceeded" in out["summary"]
    # Refused before ever reserving a concurrency slot.
    assert spawn_mod._active_agent_counts.get("sess1", 0) == 0


def test_team_create_dispatches_normally_when_within_budget(monkeypatch):
    monkeypatch.setattr("server.agents.event_bus.event_bus", _Bus())
    monkeypatch.setattr("server.agent_tools.spawn._record_agent_cost", lambda *a, **k: None)
    monkeypatch.setattr(spawn_mod, "_active_agent_counts", {})
    _no_budget_cap(monkeypatch)
    calls = []

    async def _fake_run_agent(task, **kwargs):
        calls.append(task)
        return _fake_result()

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)

    asyncio.run(
        agent_tools.execute_team_create(
            {"team_name": "t", "agents": [{"name": "a", "task": "x"}]}, "model-x", "sess1"
        )
    )
    assert calls == ["x"]


def test_spawn_agent_refuses_when_budget_already_exceeded(monkeypatch):
    monkeypatch.setattr("server.agents.event_bus.event_bus", _Bus())
    monkeypatch.setattr(spawn_mod, "_active_agent_counts", {})
    _budget_already_over(monkeypatch)

    async def _fail_if_called(task, **kwargs):
        raise AssertionError("run_agent must not be called once the budget is already exceeded")

    monkeypatch.setattr("server.agents.runtime.run_agent", _fail_if_called)

    import json

    out = json.loads(asyncio.run(agent_tools.execute_spawn_agent({"task": "work"}, "sess1", "m")))
    assert out["status"] == "failed"
    assert "Budget exceeded" in out["output"]


def test_nested_spawn_agent_refuses_when_budget_already_exceeded(monkeypatch):
    monkeypatch.setattr(spawn_mod, "_active_agent_counts", {})
    _budget_already_over(monkeypatch)

    async def _fail_if_called(task, **kwargs):
        raise AssertionError("run_agent must not be called once the budget is already exceeded")

    monkeypatch.setattr("server.agents.runtime.run_agent", _fail_if_called)

    out = asyncio.run(
        agent_tools.execute_spawn_agent(
            {"task": "work"}, "sess1", "m", parent_agent_id="caller-id", depth=1
        )
    )
    assert "Budget exceeded" in out


# ── #2: durable output persistence ──────────────────────────────────────────


def test_team_member_result_lands_in_the_task_registry(monkeypatch):
    monkeypatch.setattr("server.agents.event_bus.event_bus", _Bus())
    monkeypatch.setattr("server.agent_tools.spawn._record_agent_cost", lambda *a, **k: None)
    monkeypatch.setattr(spawn_mod, "_active_agent_counts", {})
    _no_budget_cap(monkeypatch)

    async def _fake_run_agent(task, **kwargs):
        return _fake_result(agent_id="registry-agent-1", output="the actual findings")

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)

    asyncio.run(
        agent_tools.execute_team_create(
            {"team_name": "t", "agents": [{"name": "a", "task": "investigate x"}]},
            "model-x",
            "sess1",
        )
    )

    task = registry.get_task("registry-agent-1")
    assert task is not None
    assert task["status"] == "completed"
    assert task["result_text"] == "the actual findings"
    assert task["session_id"] == "sess1"

    # And task_output actually finds it — this is the whole point: no more
    # spawning an agent to re-ask a finished sibling for its own answer.
    from server.tasks.tools import _exec_task_output

    out = _exec_task_output({"task_id": "registry-agent-1", "__session_id__": "sess1"}, "", [])
    assert "the actual findings" in out


def test_spawn_agent_result_lands_in_the_task_registry(monkeypatch):
    monkeypatch.setattr("server.agents.event_bus.event_bus", _Bus())
    monkeypatch.setattr(spawn_mod, "_active_agent_counts", {})
    _no_budget_cap(monkeypatch)

    async def _fake_run_agent(task, **kwargs):
        return _fake_result(agent_id="registry-agent-2", output="spawn_agent output")

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)

    asyncio.run(agent_tools.execute_spawn_agent({"task": "work"}, "sess1", "m"))

    task = registry.get_task("registry-agent-2")
    assert task is not None
    assert task["status"] == "completed"
    assert task["result_text"] == "spawn_agent output"


def test_a_refused_preflight_agent_is_not_persisted(monkeypatch):
    # No agent_id was ever assigned (nothing ran) — nothing to key a registry
    # row by, and _persist_agent_result must not touch the registry at all.
    monkeypatch.setattr("server.agents.event_bus.event_bus", _Bus())
    monkeypatch.setattr(spawn_mod, "_active_agent_counts", {})
    _budget_already_over(monkeypatch)

    def _fail_if_called(*a, **k):
        raise AssertionError("registry.create_task must not be called for a refused pre-flight")

    monkeypatch.setattr(registry, "create_task", _fail_if_called)

    asyncio.run(agent_tools.execute_spawn_agent({"task": "work"}, "sess1", "m"))
