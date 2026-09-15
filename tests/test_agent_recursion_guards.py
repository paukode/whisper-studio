"""Guards against uncontrolled agent fan-out: team_create must inherit the
calling agent's recursion depth (it previously always ran members at depth 0,
so a team member's own team_create never hit MAX_DEPTH), a session-wide cap
on concurrently-running agents must refuse a start past the limit instead of
launching it, and an agent at the depth limit must not see delegation tools
in its own pool at all. Mirrors a real incident where one request queued 56
agent slots and blew past a $600 daily cost cap.
"""

import asyncio
import json
from types import SimpleNamespace

from server import agent_tools
from server.agent_tools import spawn as spawn_mod
from server.agents import tools as agent_runtime_tools


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


def test_team_create_threads_depth_into_members(monkeypatch):
    monkeypatch.setattr("server.agents.event_bus.event_bus", _Bus())
    monkeypatch.setattr("server.agent_tools.spawn._record_agent_cost", lambda *a, **k: None)
    captured = []

    async def _fake_run_agent(task, **kwargs):
        captured.append(kwargs)
        return _fake_result()

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)

    asyncio.run(
        agent_tools.execute_team_create(
            {"team_name": "t", "agents": [{"name": "a", "task": "x"}]},
            "model-x",
            "sess1",
            parent_agent_id="caller-id",
            depth=2,
        )
    )

    assert captured[0]["depth"] == 3
    assert captured[0]["parent_agent_id"] == "caller-id"


def test_team_create_defaults_to_depth_zero_for_top_level_calls(monkeypatch):
    monkeypatch.setattr("server.agents.event_bus.event_bus", _Bus())
    monkeypatch.setattr("server.agent_tools.spawn._record_agent_cost", lambda *a, **k: None)
    captured = []

    async def _fake_run_agent(task, **kwargs):
        captured.append(kwargs)
        return _fake_result()

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)

    asyncio.run(
        agent_tools.execute_team_create(
            {"team_name": "t", "agents": [{"name": "a", "task": "x"}]}, "model-x", "sess1"
        )
    )

    assert captured[0]["depth"] == 1
    assert captured[0]["parent_agent_id"] is None


def test_team_create_refuses_members_past_concurrency_cap(monkeypatch):
    monkeypatch.setattr("server.agents.event_bus.event_bus", _Bus())
    monkeypatch.setattr("server.agent_tools.spawn._record_agent_cost", lambda *a, **k: None)
    monkeypatch.setattr(
        spawn_mod, "_active_agent_counts", {"sess1": spawn_mod.MAX_CONCURRENT_AGENTS_PER_SESSION}
    )

    async def _fake_run_agent(task, **kwargs):
        raise AssertionError("run_agent must not be called past the concurrency cap")

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)

    out_json, _payload = asyncio.run(
        agent_tools.execute_team_create(
            {"team_name": "t", "agents": [{"name": "a", "task": "x"}]}, "model-x", "sess1"
        )
    )
    out = json.loads(out_json)
    assert out["agents_completed"] == 1
    assert "Concurrency limit" in out["summary"]
    # The reservation must not have moved — nothing actually ran.
    assert spawn_mod._active_agent_counts["sess1"] == spawn_mod.MAX_CONCURRENT_AGENTS_PER_SESSION


def test_concurrency_slot_released_after_team_member_completes(monkeypatch):
    monkeypatch.setattr("server.agents.event_bus.event_bus", _Bus())
    monkeypatch.setattr("server.agent_tools.spawn._record_agent_cost", lambda *a, **k: None)
    monkeypatch.setattr(spawn_mod, "_active_agent_counts", {})
    seen_during_run = {}

    async def _fake_run_agent(task, **kwargs):
        seen_during_run["count"] = spawn_mod._active_agent_counts.get("sess1")
        return _fake_result()

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)

    asyncio.run(
        agent_tools.execute_team_create(
            {"team_name": "t", "agents": [{"name": "a", "task": "x"}]}, "model-x", "sess1"
        )
    )

    assert seen_during_run["count"] == 1
    assert spawn_mod._active_agent_counts.get("sess1", 0) == 0


def test_spawn_agent_refuses_past_concurrency_cap(monkeypatch):
    monkeypatch.setattr("server.agents.event_bus.event_bus", _Bus())
    monkeypatch.setattr(
        spawn_mod,
        "_active_agent_counts",
        {"sess1": spawn_mod.MAX_CONCURRENT_AGENTS_PER_SESSION},
    )

    async def _fake_run_agent(task, **kwargs):
        raise AssertionError("run_agent must not be called past the concurrency cap")

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)

    out = json.loads(asyncio.run(agent_tools.execute_spawn_agent({"task": "work"}, "sess1", "m")))
    assert out["status"] == "failed"
    assert "Concurrency limit" in out["output"]


def test_strip_delegation_tools_below_depth_limit_is_a_no_op():
    tools = [{"name": "spawn_agent"}, {"name": "team_create"}, {"name": "ws_grep"}]
    out = agent_runtime_tools.strip_delegation_tools_at_depth_limit(tools, depth=3)
    assert out == tools


def test_strip_delegation_tools_at_depth_limit_removes_both():
    tools = [{"name": "spawn_agent"}, {"name": "team_create"}, {"name": "ws_grep"}]
    out = agent_runtime_tools.strip_delegation_tools_at_depth_limit(tools, depth=4)
    names = {t["name"] for t in out}
    assert names == {"ws_grep"}


def test_team_create_concurrency_cap_matches_the_one_agent_concurrency_knob():
    """See tests/test_audit_remainder.py: AGENT_CALL_CONCURRENCY is the ONE
    knob every agent model call is throttled through, kept equal to
    WORKFLOW_MAX_CONCURRENCY after a real past mismatch let agents get
    dispatched against a too-small pool. A THIRD, independently-chosen
    number here for team_create/spawn_agent's own session-wide cap would
    reintroduce exactly that class of bug for this path."""
    from server.agents.providers.base import AGENT_CALL_CONCURRENCY

    assert spawn_mod.MAX_CONCURRENT_AGENTS_PER_SESSION == AGENT_CALL_CONCURRENCY
