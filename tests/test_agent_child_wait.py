"""An agent's time limit covers its own work, not waiting on agents it starts.

Each agent a call starts (spawn_agent, team_create, skill_invoke) runs on its
own round and time limits, so the executor adds the time the call waits on
them to the run's "waited" total (ToolScope.waited), the runner moves the
deadline by it, and the card's time bar shows only the run's own time. The
run-level cases drive the real run_agent on a simulated clock.
"""

import asyncio
import json
import os
import time as real_time
from concurrent.futures import ThreadPoolExecutor

import pytest

import server.agents.runtime as runtime_mod
import server.chat.engine.runner as runner_mod
import server.tool_executor as te
from server.agents import journal as journal_mod
from server.agents.config import AgentConfig, with_run_limits
from server.agents.runtime import run_agent
from server.agents.runtime_support import budget_readout
from server.agents.tool_access import WAITS_ON_AGENTS, ToolScope
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block

START = 10_000.0
LIMIT_MINUTES = 15


class _Clock:
    now = START


class _FakeTime:
    def __getattr__(self, name):
        return getattr(real_time, name)

    @staticmethod
    def monotonic():
        return _Clock.now


def _minutes() -> float:
    return (_Clock.now - START) / 60


@pytest.fixture(autouse=True)
def simulated_clock(monkeypatch, tmp_path):
    _Clock.now = START
    for module in (runner_mod, runtime_mod, te):
        monkeypatch.setattr(module, "time", _FakeTime())
    monkeypatch.setattr(journal_mod, "storage_root", lambda: str(tmp_path / "storage"))
    monkeypatch.setattr("server.costs.budget.check_budget", lambda session_id: None)
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    monkeypatch.setattr(
        "server.infrastructure.run_limits.time_limit_seconds", lambda: LIMIT_MINUTES * 60.0
    )


class _Model(FakeBedrockClient):
    """Calls ``tool`` each round while it is offered tools, ``calls`` times;
    after that, or with no tools offered (the forced last round), it writes
    its report. Each model call takes ``round_minutes`` of its own time."""

    def __init__(self, tool: str, *, calls: int, round_minutes: float = 0.0):
        super().__init__([])
        self.tool, self.calls, self.round_minutes, self.n = tool, calls, round_minutes, 0

    def invoke_model_with_response_stream(self, modelId, contentType, accept, body):
        _Clock.now += self.round_minutes * 60
        if json.loads(body).get("tools") and self.n < self.calls:
            call = tool_use_block(f"t{self.n}", self.tool, {"task": f"part {self.n}"})
            self._rounds.append([msg_start(), *call, *msg_end(stop_reason="tool_use")])
            self.n += 1
        else:
            self._rounds.append([msg_start(), *text_block("Report."), *msg_end()])
        return super().invoke_model_with_response_stream(modelId, contentType, accept, body)


def _run_coordinator(monkeypatch, model: _Model, *, worker_minutes: float = 12):
    started: list[float] = []

    async def route(name, tool_input, **kwargs):
        if name in WAITS_ON_AGENTS:
            started.append(_minutes())
            _Clock.now += worker_minutes * 60  # the agent it started runs this long
        return "done", []

    monkeypatch.setattr(te, "route_tool", route)
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: model)
    result = asyncio.run(
        run_agent("coordinate", agent_type="coordinator", session_id="sim", model_id_override="m")
    )
    return result, started


def _turn_start_elapsed(agent_id: str) -> list[float]:
    path = os.path.join(journal_mod.load(agent_id)["dir"], "events.jsonl")
    events = [json.loads(line) for line in open(path)]
    return [e["elapsed_s"] for e in events if e.get("phase") == "turn_start"]


def test_a_coordinator_waiting_on_its_workers_is_not_stopped_by_the_time_limit(monkeypatch):
    """Four 12-minute workers: 48 minutes of waiting, far past a 15-minute
    limit counted on the wall clock, and the coordinator still finishes."""
    result, started = _run_coordinator(monkeypatch, _Model("spawn_agent", calls=4))

    assert len(started) == 4
    assert _minutes() >= 4 * 12
    assert result.stop_reason == "completed"
    # The card's time bar shows its own time only.
    assert max(_turn_start_elapsed(result.agent_id)) < 60


def test_its_own_work_still_runs_on_the_time_limit(monkeypatch):
    """The control: rounds that take its own time, with nothing to wait on,
    end the run at the limit."""
    result, started = _run_coordinator(
        monkeypatch, _Model("list_agents", calls=20, round_minutes=5)
    )

    assert started == []
    assert result.stop_reason == "deadline"
    assert _minutes() < 2 * LIMIT_MINUTES


@pytest.mark.parametrize("tool,waits", [("spawn_agent", True), ("ws_read_file", False)])
def test_only_calls_that_wait_on_agents_add_to_the_waited_total(monkeypatch, tool, waits):
    async def route(name, tool_input, **kwargs):
        _Clock.now += 60
        return "ok", []

    monkeypatch.setattr(te, "route_tool", route)
    budget = {"rounds": 0, "seconds": 0.0, "waited": 0.0}
    scope = ToolScope(permitted=frozenset({tool}), activation_key="agent:x", budget=budget)
    executor = ThreadPoolExecutor(max_workers=1)

    async def batch():
        return await te.execute_tool_batch(
            [{"id": "a", "name": tool, "input": {}}],
            is_concurrent_safe=lambda n: False,
            loop=asyncio.get_running_loop(),
            executor=executor,
            transcript="",
            attachments=None,
            session_id="s1",
            session_denials={},
            model_id="m",
            plan_mode=False,
            tool_scope=scope,
        )

    try:
        asyncio.run(batch())
    finally:
        executor.shutdown(wait=False)
    assert budget["waited"] == (60.0 if waits else 0.0)


def test_the_card_counts_only_the_agents_own_time():
    readout = budget_readout(
        with_run_limits(AgentConfig()),
        {"rounds": 0, "seconds": 0.0, "waited": 600.0},
        next_turn=2,
        elapsed=660.0,
        usage={"cost_usd": 0.0},
        cost_capped=False,
    )
    assert readout["elapsed_s"] == 60.0
    assert readout["budget_state"] == "working"
