"""An agent's spend reaches the cost log exactly once.

The turn engine records every agent round under the parent session as it
runs. spawn_agent, team members and the /subagent stream used to add a second
'<key>_agent' (or '_subagent') row holding the same cumulative totals when the
agent finished, so every agent run was counted twice.
"""

import asyncio
import json
from types import SimpleNamespace

from server import agent_tools
from server.agent_tools import spawn as spawn_mod
from server.costs import budget as budget_mod
from server.costs import tracker


class _Bus:
    def publish(self, channel, ev):
        pass


_USAGE = {
    "input_tokens": 1_000,
    "output_tokens": 100,
    "cache_read_tokens": 800,
    "cache_creation_tokens": 0,
}


def _fake_run_agent_recording_one_round(session_rows: list):
    async def _run(task, **kwargs):
        # What the engine does for the agent's single round.
        tracker.record_turn(
            session_id=kwargs["session_id"],
            turn_number=0,
            model="gpt5.6-sol",
            input_tokens=_USAGE["input_tokens"],
            output_tokens=_USAGE["output_tokens"],
            cache_read_tokens=_USAGE["cache_read_tokens"],
            source="agent",
        )
        session_rows.append(kwargs["session_id"])
        return SimpleNamespace(
            agent_id="a1",
            agent_type="general",
            status="completed",
            turns_used=1,
            tools_called=[],
            usage=dict(_USAGE),
            output="done",
        )

    return _run


def _setup(monkeypatch):
    monkeypatch.setattr("server.agents.event_bus.event_bus", _Bus())
    monkeypatch.setattr(spawn_mod, "_active_agent_counts", {})
    monkeypatch.setattr(budget_mod, "check_budget", lambda session_id: None)
    ran: list = []
    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent_recording_one_round(ran))
    return ran


def test_spawn_agent_adds_no_rollup_row(monkeypatch):
    ran = _setup(monkeypatch)
    out = json.loads(
        asyncio.run(agent_tools.execute_spawn_agent({"task": "work"}, "sess-once", "m"))
    )
    assert out["status"] == "completed" and ran == ["sess-once"]
    rows = tracker.get_session_costs("sess-once")
    assert [r["model"] for r in rows] == ["gpt5.6-sol"]


def test_team_member_adds_no_rollup_row(monkeypatch):
    _setup(monkeypatch)
    asyncio.run(
        agent_tools.execute_team_create(
            {"team_name": "t", "agents": [{"name": "a", "task": "x"}]}, "model-x", "sess-team"
        )
    )
    rows = tracker.get_session_costs("sess-team")
    assert [r["model"] for r in rows] == ["gpt5.6-sol"]
