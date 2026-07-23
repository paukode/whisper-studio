"""WS-D slice 3: the run manager — registry row lifecycle, completion event,
list/get, stop, and boot reconcile. Drives the real harness with a fake agent
runner so no Bedrock is touched.
"""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest

NODE = shutil.which("node") or "/usr/local/bin/node"
HARNESS_OK = os.path.exists(NODE) and os.path.exists(
    os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "server", "workflows", "harness", "harness.mjs"
    )
)
pytestmark = pytest.mark.skipif(not HARNESS_OK, reason="node/harness missing")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    from server.infrastructure import sessions

    storage = tmp_path / "storage"
    os.makedirs(storage, exist_ok=True)
    monkeypatch.setattr(sessions, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(sessions, "DB_PATH", str(storage / "sessions.db"))
    from server.workflows import manager

    manager._live.clear()
    yield
    manager._live.clear()


async def _await_done(run_id, timeout=15):
    from server.workflows import manager

    for _ in range(int(timeout / 0.1)):
        run = manager.get_run(run_id)
        if run and run["status"] in ("done", "failed", "stopped", "stale"):
            return run
        await asyncio.sleep(0.1)
    raise AssertionError("run did not finish")


def test_manager_run_lifecycle():
    from server.workflows import manager

    async def fake_agent(prompt, opts):
        return {
            "text": f"did:{prompt}",
            "usage": {"input_tokens": 2, "output_tokens": 1},
            "status": "completed",
        }

    src = (
        "export const meta = { name: 'demo', description: 'd', phases: ['work'] }\n"
        "phase('work')\n"
        "const r = await agent('the task')\n"
        "return { echo: r.text }\n"
    )

    async def go():
        rid = manager.start_run(
            src,
            session_id="s1",
            model_key="sonnet",
            name="demo",
            phases=["work"],
            agent_runner=fake_agent,
        )
        assert rid
        run = await _await_done(rid)
        assert run["status"] == "done"
        assert run["result"] == {"echo": "did:the task"}
        assert run["agents_spawned"] == 1
        assert any(e.get("type") == "agent_call" for e in run["journal"])
        return rid

    rid = asyncio.run(go())

    # It shows up in the session list.
    runs = manager.list_runs("s1")
    assert any(r["run_id"] == rid and r["name"] == "demo" for r in runs)


def test_manager_run_failure_recorded():
    from server.workflows import manager

    async def fake_agent(prompt, opts):
        return {"text": "x", "usage": {}, "status": "completed"}

    src = (
        "export const meta = { name: 'boom', description: 'd', phases: [] }\n"
        "throw new Error('kaboom')\n"
    )

    async def go():
        rid = manager.start_run(src, session_id="s2", agent_runner=fake_agent)
        run = await _await_done(rid)
        assert run["status"] == "failed"
        assert "kaboom" in (run["error"] or "")

    asyncio.run(go())


def test_reconcile_stale():
    from server.workflows import manager

    # Insert a phantom 'running' row (as if the server died mid-run).
    with manager._conn() as conn:
        manager._ensure_table(conn)
        conn.execute(
            "INSERT INTO workflow_runs (run_id, status, started_at) VALUES ('ghost','running','2026')"
        )
    n = manager.reconcile_stale()
    assert n >= 1
    assert manager.get_run("ghost")["status"] == "stale"
