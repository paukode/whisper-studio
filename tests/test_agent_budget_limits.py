"""Soft limits and live extensions: the final (reporting) round starts at 90
percent of a time or cost budget, a running agent's cap can be raised from
outside, and every turn_start event carries the budget readout the cards draw."""

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from server.agents import extensions
from server.agents import journal as journal_mod
from server.agents.config import get_agent_config
from server.agents.event_bus import event_bus
from server.agents.runtime import run_agent
from server.tasks import registry, shell
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_mod, "storage_root", lambda: str(tmp_path / "storage"))
    monkeypatch.setattr(registry, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))
    monkeypatch.setattr("server.costs.budget.check_budget", lambda session_id: None)
    monkeypatch.setattr(
        "server.costs.budget.check_budget_soft", lambda session_id, fraction=0.9: None
    )


def _patch_engine(monkeypatch, fake_stream, route_tool=None):
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool", lambda *a, **k: ([], [], 0)
    )
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)

    async def _default(tool_name, tool_input, **kw):
        return "ok", []

    monkeypatch.setattr("server.tool_executor.route_tool", route_tool or _default)


def _tool_round(tool_id: str):
    return [
        msg_start(),
        *tool_use_block(tool_id, "ws_grep", {"pattern": "x"}),
        *msg_end("tool_use"),
    ]


def _text_round(text: str):
    return [msg_start(), *text_block(text), *msg_end(stop_reason="end_turn")]


def test_time_budget_starts_the_report_round_at_ninety_percent(monkeypatch):
    """Two slow tool rounds cross 90 percent of a 1 s budget; the third round
    is the finale (tools off, report), and the reason is named as deadline."""

    async def _slow(tool_name, tool_input, **kw):
        await asyncio.sleep(0.5)
        return "ok", []

    _patch_engine(
        monkeypatch,
        FakeBedrockClient([_tool_round("t1"), _tool_round("t2"), _text_round("FINDINGS: done.")]),
        route_tool=_slow,
    )
    cfg = dataclasses.replace(get_agent_config("general"), max_turns=10, deadline_seconds=1.0)
    result = asyncio.run(run_agent("go", config=cfg, session_id="s1", model_id_override="m"))
    assert result.stop_reason == "deadline"
    assert result.output.startswith("[Agent stopped - reached time limit]")
    assert "FINDINGS: done." in result.output
    assert result.turns_used == 3


def test_cost_soft_limit_starts_the_report_round(monkeypatch):
    calls = {"n": 0}

    def _soft(session_id, fraction=0.9):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return SimpleNamespace(
            message="Session cost $0.91 has reached 90 percent of the limit of $1.00",
            kind="session",
            limit=1.0,
            current=0.91,
        )

    monkeypatch.setattr("server.costs.budget.check_budget_soft", _soft)
    _patch_engine(monkeypatch, FakeBedrockClient([_tool_round("t1"), _text_round("FINDINGS: x.")]))
    result = asyncio.run(
        run_agent("go", agent_type="general", session_id="s1", model_id_override="m")
    )
    assert result.stop_reason == "cost_cap"
    assert result.output.startswith("[Agent stopped - reached the session cost cap]")
    assert "FINDINGS: x." in result.output
    assert result.turns_used == 2


def test_extension_raises_a_running_agents_cap(monkeypatch):
    """max_turns=1 would make round 2 the finale; a grant of two rounds from
    the first tool call lets the agent run three rounds, and the turn-limit
    note names the extended cap."""

    granted = {"done": False}

    async def _extend_on_first_call(tool_name, tool_input, **kw):
        if not granted["done"]:
            granted["done"] = True
            assert extensions.extend("ext-me", rounds=2) == {"rounds": 2, "seconds": 0.0}
        return "ok", []

    _patch_engine(
        monkeypatch,
        FakeBedrockClient([_tool_round("t1"), _tool_round("t2"), _text_round("FINDINGS: y.")]),
        route_tool=_extend_on_first_call,
    )
    cfg = dataclasses.replace(get_agent_config("general"), max_turns=1, deadline_seconds=None)
    result = asyncio.run(
        run_agent("go", config=cfg, session_id="s1", model_id_override="m", agent_id="ext-me")
    )
    assert result.turns_used == 3
    assert result.stop_reason == "turn_limit"
    assert "reached turn limit (3)" in result.output
    assert extensions.get("ext-me") is None  # unregistered once the run ends


def test_turn_start_events_carry_the_budget_readout(monkeypatch):
    _patch_engine(monkeypatch, FakeBedrockClient([_tool_round("t1"), _text_round("FINDINGS: z.")]))
    cfg = dataclasses.replace(get_agent_config("general"), max_turns=2, deadline_seconds=600.0)

    async def _run():
        queue = event_bus.subscribe("s-budget")
        try:
            await run_agent("go", config=cfg, session_id="s-budget", model_id_override="m")
            await asyncio.sleep(0)
            out = []
            while not queue.empty():
                out.append(queue.get_nowait())
            return out
        finally:
            event_bus.unsubscribe("s-budget", queue)

    events = asyncio.run(_run())
    started = next(e for e in events if e.get("phase") == "started")
    assert started["max_turns"] == 2 and started["deadline_s"] == 600.0
    turn_starts = [e for e in events if e.get("phase") == "turn_start"]
    assert turn_starts, "no turn_start events"
    first = turn_starts[0]
    assert first["turn"] == 2 and first["max_turns"] == 2
    assert first["deadline_s"] == 600.0 and isinstance(first["elapsed_s"], float)
    assert "cost_usd" in first
    # Round 2 of 2 is the final round: the card shows "finishing up".
    assert first["budget_state"] == "finishing"


def test_extend_endpoint_grants_only_to_running_agents():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.agents.routes import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    ext = extensions.new_extension()
    extensions.register("live-1", ext)
    try:
        r = client.post("/api/agents/live-1/extend", json={"rounds": 20})
        assert r.status_code == 200
        assert r.json()["extension"] == {"rounds": 20, "seconds": 0.0}
        assert ext["rounds"] == 20
        r = client.post("/api/agents/live-1/extend", json={"seconds": 300})
        assert r.json()["extension"] == {"rounds": 20, "seconds": 300.0}
        assert client.post("/api/agents/live-1/extend", json={}).status_code == 400
        assert client.post("/api/agents/gone/extend", json={"rounds": 5}).status_code == 404
    finally:
        extensions.unregister("live-1")


def test_check_budget_soft_trips_before_the_cap(monkeypatch):
    from server.costs import budget

    monkeypatch.setattr(budget, "load_config", lambda: {"max_session_cost_usd": 1.0})
    monkeypatch.setattr(budget, "get_session_summary", lambda sid: {"total_cost_usd": 0.92})
    monkeypatch.setattr(budget, "get_today_total_cost", lambda: 0.0)
    # The autouse fixture stubs the public entry points; exercise the shared
    # check directly at both fractions.
    assert budget._check("s", 1.0) is None
    soft = budget._check("s", 0.9)
    assert soft is not None and soft.kind == "session"
    assert "90 percent of the limit" in soft.message
