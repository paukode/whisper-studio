"""WS-D slice 3: WorkflowRun drives the real Node harness with a FAKE agent
runner (no Bedrock). Exercises the ledger, the 1000-agent cap, the USD budget,
and the resume cache — the server-side limits the script cannot bypass.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections import deque

import pytest

NODE = shutil.which("node") or ""
HARNESS_OK = os.path.exists(NODE) and os.path.exists(
    os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "server",
        "workflows",
        "harness",
        "harness.mjs",
    )
)
pytestmark = pytest.mark.skipif(not HARNESS_OK, reason="node/harness missing")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    yield


def _run(coro):
    return asyncio.run(coro)


def _mk(source, **kw):
    from server.workflows.runtime import WorkflowRun

    return WorkflowRun("run-test", source, **kw)


def test_runtime_executes_and_accounts():
    # Every agent run reports $1 (the engine's per-round pricing), so the
    # ledger total is deterministic.
    calls = []

    async def fake_agent(prompt, opts):
        calls.append(prompt)
        return {
            "text": f"ran:{prompt}",
            "output": None,
            "usage": {"input_tokens": 5, "output_tokens": 3, "cost_usd": 1.0},
            "status": "completed",
        }

    src = (
        "export const meta = { name: 'x', description: 'y', phases: ['a'] }\n"
        "phase('a')\n"
        "const r = await parallel([() => agent('one'), () => agent('two'), () => agent('three')])\n"
        "return r.map(x => x.text)\n"
    )
    run = _mk(src, agent_runner=fake_agent)
    out = _run(run.run())
    assert out["status"] == "done"
    assert set(out["result"]) == {"ran:one", "ran:two", "ran:three"}
    assert out["agents_spawned"] == 3
    assert out["tokens_in"] == 15 and out["tokens_out"] == 9
    assert out["cost_usd"] == 3.0
    assert len(calls) == 3


def test_agent_cap_enforced(monkeypatch):
    import server.workflows.runtime as RT

    monkeypatch.setattr(RT, "WORKFLOW_MAX_AGENTS", 2)

    async def fake_agent(prompt, opts):
        return {"text": "ok", "usage": {}, "status": "completed"}

    # The script keeps spawning until the cap error, then reports how many.
    src = (
        "export const meta = { name: 'x', description: 'y', phases: [] }\n"
        "let n = 0;\n"
        "try { for (let i=0;i<10;i++){ await agent('a'+i); n++; } }\n"
        "catch (e) { return { n, capped: e.type || e.message }; }\n"
        "return { n, capped: false };\n"
    )
    run = _mk(src, agent_runner=fake_agent)
    out = _run(run.run())
    assert out["status"] == "done"
    assert out["result"]["n"] == 2  # exactly two ran before the cap
    assert "AgentCap" in str(out["result"]["capped"])
    assert out["cap_reached"] is True


def test_budget_enforced():
    async def fake_agent(prompt, opts):
        return {
            "text": "ok",
            "usage": {"input_tokens": 1, "output_tokens": 5},
            "status": "completed",
        }

    # budget 8 output tokens: first agent (5) runs, second runs too (5 < 8,
    # total 10), third is rejected (10 >= 8). Assert it stops.
    src = (
        "export const meta = { name: 'x', description: 'y', phases: [] }\n"
        "let n = 0;\n"
        "try { for (let i=0;i<10;i++){ await agent('a'+i); n++; } }\n"
        "catch (e) { return { n, err: e.type }; }\n"
        "return { n, err: false };\n"
    )
    run = _mk(src, agent_runner=fake_agent, budget_tokens=8)
    out = _run(run.run())
    assert out["status"] == "done"
    assert out["result"]["err"] == "BudgetExceededError"
    assert out["result"]["n"] == 2  # two ran (10 tokens), third over budget


def test_resume_cache_replays_without_running(monkeypatch):
    from server.workflows.journal import call_hash

    ran = {"n": 0}

    async def fake_agent(prompt, opts):
        ran["n"] += 1
        return {"text": "LIVE", "usage": {}, "status": "completed"}

    src = (
        "export const meta = { name: 'x', description: 'y', phases: [] }\n"
        "const a = await agent('cached prompt', { schema: { type: 'object' } })\n"
        "return a.text\n"
    )
    h = call_hash("cached prompt", {"schema": {"type": "object"}})
    cache = {h: deque([{"text": "FROM-CACHE", "output": None, "usage": {}, "status": "completed"}])}
    run = _mk(src, agent_runner=fake_agent, resume_cache=cache)
    out = _run(run.run())
    assert out["status"] == "done"
    assert out["result"] == "FROM-CACHE"
    assert ran["n"] == 0  # live runner never called — pure cache hit


def test_events_emitted(monkeypatch):
    events = []

    async def fake_agent(prompt, opts):
        return {"text": "ok", "usage": {}, "status": "completed"}

    src = (
        "export const meta = { name: 'x', description: 'y', phases: ['work'] }\n"
        "phase('work')\n"
        "await agent('go', { label: 'scout' })\n"
        "return 1\n"
    )
    run = _mk(src, agent_runner=fake_agent, on_event=lambda ev: events.append(ev))
    out = _run(run.run())
    assert out["status"] == "done"
    kinds = {e["type"] for e in events}
    assert "phase" in kinds and "agent" in kinds
    assert any(e.get("label") == "scout" for e in events if e["type"] == "agent")


def test_agent_calls_journal_their_spans_and_phase():
    """Each agent call journals when it started and finished, so the run card
    can read how many ran at once; agent events carry the phase both ways."""
    from server.workflows import journal as jnl
    from server.workflows.manager import agent_call_rows

    events = []

    async def fake_agent(prompt, opts):
        await asyncio.sleep(0.05)
        return {"text": "ok", "usage": {}, "status": "completed", "agent_id": f"id-{prompt}"}

    src = (
        "export const meta = { name: 'x', description: 'y', phases: ['work'] }\n"
        "phase('work')\n"
        "await parallel([() => agent('a'), () => agent('b')])\n"
        "return 1\n"
    )
    run = _mk(src, agent_runner=fake_agent, on_event=lambda ev: events.append(ev))
    assert _run(run.run())["status"] == "done"
    rows = agent_call_rows(jnl.read_journal("run-test"))
    assert [r["agent_id"] for r in sorted(rows, key=lambda r: r["agent_id"])] == ["id-a", "id-b"]
    assert all("text" not in r and "output" not in r for r in rows)
    for r in rows:
        assert r["started_at"] < r["finished_at"]
    # The two parallel calls overlap in time.
    a, b = rows
    assert a["started_at"] < b["finished_at"] and b["started_at"] < a["finished_at"]
    agent_events = [e for e in events if e["type"] == "agent"]
    assert agent_events and all(e.get("phase") == "work" for e in agent_events)
