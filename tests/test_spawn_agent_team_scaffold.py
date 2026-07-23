"""Inline spawn_agent wraps its agent in a one-member team so the UI renders
the rich per-agent tool log (team_started/team_completed + team_id passed to
run_agent), instead of the detail-less AgentCard summary.
"""

import asyncio
import json
from types import SimpleNamespace

from server import agent_tools


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, channel, ev):
        self.events.append((channel, ev))


def test_spawn_agent_emits_team_scaffold(monkeypatch):
    bus = _Bus()
    monkeypatch.setattr("server.agents.event_bus.event_bus", bus)
    captured = {}

    async def _fake_run_agent(task, **kwargs):
        captured.update(kwargs, task=task)
        return SimpleNamespace(
            agent_id="a1",
            agent_type="general",
            status="completed",
            turns_used=3,
            tools_called=["ws_grep"],
            usage={},
            output="done",
        )

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)
    monkeypatch.setattr("server.agent_tools.spawn._record_agent_cost", lambda *a, **k: None)

    out = json.loads(
        asyncio.run(
            agent_tools.execute_spawn_agent(
                {"task": "Catalog the docs"}, "sess1", "model-x", effort_label="high"
            )
        )
    )

    # run_agent received a team_id + agent_name so its per-agent events fold.
    assert captured["team_id"]
    assert captured["agent_name"] == "Catalog the docs"
    # The tool result carries team_id (anchor for the report).
    assert out["team_id"] == captured["team_id"]

    phases = [(ch, ev.get("phase"), ev.get("team_id")) for ch, ev in bus.events]
    assert ("sess1", "team_started", captured["team_id"]) in phases
    assert ("sess1", "team_completed", captured["team_id"]) in phases

    started = next(ev for ch, ev in bus.events if ev.get("phase") == "team_started")
    assert started["agents"][0]["task"] == "Catalog the docs"
    assert started["agents"][0]["role"] == "team"


def test_spawn_label_truncates_and_takes_first_line():
    from server.agent_tools.spawn import _spawn_label

    assert _spawn_label("short task") == "short task"
    assert _spawn_label("first line\nsecond line") == "first line"
    long = "x" * 100
    out = _spawn_label(long)
    assert out.endswith("…") and len(out) <= 60


def test_spawn_agent_registers_stoppable_team(monkeypatch):
    # While the agent runs, _teams[team_id] must hold the cancellable task so
    # POST /api/teams/{team_id}/stop works (the card's Stop button was a silent
    # no-op before). After completion the task handle is removed.
    import asyncio

    from server.agent_tools.teams import _teams

    bus = _Bus()
    monkeypatch.setattr("server.agents.event_bus.event_bus", bus)
    monkeypatch.setattr("server.agent_tools.spawn._record_agent_cost", lambda *a, **k: None)
    seen = {}

    async def _fake_run_agent(task, **kwargs):
        tid = kwargs["team_id"]
        seen["registered_while_running"] = "task" in _teams.get(tid, {})
        return SimpleNamespace(
            agent_id="a1",
            agent_type="general",
            status="completed",
            turns_used=1,
            tools_called=[],
            usage={},
            output="ok",
        )

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)
    out = json.loads(asyncio.run(agent_tools.execute_spawn_agent({"task": "work"}, "sess1", "m")))
    assert seen["registered_while_running"] is True
    assert "task" not in _teams.get(out["team_id"], {})


def test_spawn_agent_team_completed_fires_on_exception(monkeypatch):
    # run_agent raising must still publish team_completed, or the card is
    # persisted stuck at "running" forever.
    import asyncio

    import pytest

    bus = _Bus()
    monkeypatch.setattr("server.agents.event_bus.event_bus", bus)
    monkeypatch.setattr("server.agent_tools.spawn._record_agent_cost", lambda *a, **k: None)

    async def _boom(task, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr("server.agents.runtime.run_agent", _boom)
    with pytest.raises(RuntimeError):
        asyncio.run(agent_tools.execute_spawn_agent({"task": "work"}, "sess1", "m"))
    assert any(ev.get("phase") == "team_completed" for _ch, ev in bus.events)


def test_spawn_agent_preflight_failure_publishes_failed_row(monkeypatch):
    # Pre-flight failures (retention gate, no cloud model) return failed with
    # NO per-agent events — the scaffold must publish a synthetic failed event
    # so the row doesn't sit pending forever under a completed team.
    import asyncio

    bus = _Bus()
    monkeypatch.setattr("server.agents.event_bus.event_bus", bus)
    monkeypatch.setattr("server.agent_tools.spawn._record_agent_cost", lambda *a, **k: None)

    async def _gated(task, **kwargs):
        return SimpleNamespace(
            agent_id="",
            agent_type="general",
            status="failed",
            turns_used=0,
            tools_called=[],
            usage={},
            output="[Data retention required] enable retention",
        )

    monkeypatch.setattr("server.agents.runtime.run_agent", _gated)
    asyncio.run(agent_tools.execute_spawn_agent({"task": "work"}, "sess1", "m"))
    failed = [ev for _ch, ev in bus.events if ev.get("phase") == "failed"]
    assert failed and "retention" in failed[0]["error"].lower()


def test_spawn_agent_detach_does_not_scaffold(monkeypatch):
    # Detached runs stream to their own private channel (task-events) and render
    # as a background-task card, not a team report — no team scaffold here.
    bus = _Bus()
    monkeypatch.setattr("server.agents.event_bus.event_bus", bus)
    monkeypatch.setattr("server.tasks.agents.start_detached_agent", lambda task, **k: "task-xyz")
    out = json.loads(
        asyncio.run(
            agent_tools.execute_spawn_agent({"task": "bg work", "detach": True}, "sess1", "model-x")
        )
    )
    assert out["task_id"] == "task-xyz"
    assert not any(ev.get("phase") == "team_started" for _ch, ev in bus.events)
