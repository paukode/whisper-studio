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

NODE = shutil.which("node") or "/usr/local/bin/node"
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


def test_runtime_executes_and_accounts(monkeypatch):
    # every agent "costs" $1 so we can assert the ledger deterministically.
    import server.costs.tracker as T

    monkeypatch.setattr(T, "estimate_cost", lambda *a, **k: 1.0)

    calls = []

    async def fake_agent(prompt, opts):
        calls.append(prompt)
        return {
            "text": f"ran:{prompt}",
            "output": None,
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "status": "completed",
        }

    src = (
        "export const meta = { name: 'x', description: 'y', phases: ['a'] }\n"
        "phase('a')\n"
        "const r = await parallel([() => agent('one'), () => agent('two'), () => agent('three')])\n"
        "return r.map(x => x.text)\n"
    )
    run = _mk(src, agent_runner=fake_agent, model_key="sonnet")
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


def test_budget_enforced(monkeypatch):
    import server.costs.tracker as T

    monkeypatch.setattr(T, "estimate_cost", lambda *a, **k: 5.0)  # $5 per agent

    async def fake_agent(prompt, opts):
        return {
            "text": "ok",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "status": "completed",
        }

    # budget $8: first agent ($5) runs, second is rejected (cost 5 >= 8? no —
    # 5 < 8 so it runs too → 10; third rejected). Assert it stops.
    src = (
        "export const meta = { name: 'x', description: 'y', phases: [] }\n"
        "let n = 0;\n"
        "try { for (let i=0;i<10;i++){ await agent('a'+i); n++; } }\n"
        "catch (e) { return { n, err: e.type }; }\n"
        "return { n, err: false };\n"
    )
    run = _mk(src, agent_runner=fake_agent, budget_usd=8.0, model_key="sonnet")
    out = _run(run.run())
    assert out["status"] == "done"
    assert out["result"]["err"] == "BudgetExceededError"
    assert out["result"]["n"] == 2  # two ran (cost 10), third over budget


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
