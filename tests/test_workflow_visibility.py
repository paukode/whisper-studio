"""A launched workflow has to be VISIBLE — in a list, and when it finishes.

Before this, a run was reachable only through the inline chat card that started
it: there was no runs endpoint at all (the module docstring promised one), and
though the task registry has allowed ``kind="workflow"`` from the start and
completion_inject already reports finished workflow tasks back to the model,
nothing ever created such a row. Both were dead code, so a detached swarm ended
in silence: no Background Tasks entry, no chat card, no next-turn report.

Drives the real harness with agent-free scripts, so no provider is touched.
"""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

NODE = shutil.which("node") or "/usr/local/bin/node"
HARNESS_OK = os.path.exists(NODE) and os.path.exists(
    os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "server", "workflows", "harness", "harness.mjs"
    )
)
pytestmark = pytest.mark.skipif(not HARNESS_OK, reason="node/harness missing")

_NOOP = (
    "export const meta = { name: 'noop', description: 'no agents', phases: ['a'] }\n"
    "phase('a')\n"
    "return { ok: 1 }\n"
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    from server.infrastructure import sessions

    storage = tmp_path / "storage"
    os.makedirs(storage, exist_ok=True)
    monkeypatch.setattr(sessions, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(sessions, "DB_PATH", str(storage / "sessions.db"))
    # The task registry resolves its OWN DB_PATH from storage_root() at import
    # time, so patching sessions alone leaves it writing to the running
    # checkout's real database — rows would leak between tests (and into a
    # developer's app data).
    from server.tasks import registry

    monkeypatch.setattr(registry, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(registry, "DB_PATH", str(storage / "sessions.db"))
    from server.workflows import manager

    manager._live.clear()
    manager._task_ids.clear()
    yield
    manager._live.clear()
    manager._task_ids.clear()


def _client() -> TestClient:
    from server.workflows.routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


async def _await_done(run_id, timeout=20):
    from server.workflows import manager

    for _ in range(int(timeout / 0.1)):
        run = manager.get_run(run_id)
        if run and run["status"] in ("done", "failed", "stopped", "stale"):
            return run
        await asyncio.sleep(0.1)
    raise AssertionError("run did not finish")


# ── the runs list ────────────────────────────────────────────────────────────


def test_runs_endpoint_lists_every_run_newest_first():
    from server.workflows import manager

    older = manager.start_run(_NOOP, session_id="s1", name="older")
    newer = manager.start_run(_NOOP, session_id="s2", name="newer")

    body = _client().get("/api/workflows/runs").json()
    ids = [r["run_id"] for r in body["runs"]]

    # Both sessions' runs, not just one — this panel is app-wide.
    assert older in ids and newer in ids
    assert ids.index(newer) <= ids.index(older)


def test_runs_endpoint_scopes_to_a_session_when_asked():
    from server.workflows import manager

    mine = manager.start_run(_NOOP, session_id="s1", name="mine")
    theirs = manager.start_run(_NOOP, session_id="s2", name="theirs")

    ids = [r["run_id"] for r in _client().get("/api/workflows/runs?session_id=s1").json()["runs"]]
    assert mine in ids
    assert theirs not in ids


def test_runs_endpoint_is_empty_not_an_error_with_no_runs():
    assert _client().get("/api/workflows/runs").json() == {"runs": []}


# ── the background-task mirror ───────────────────────────────────────────────


def _workflow_tasks(session_id: str) -> list[dict]:
    from server.tasks import registry

    return [t for t in registry.list_tasks(session_id=session_id) if t.get("kind") == "workflow"]


def test_launching_registers_a_background_task():
    """Launched from inside the loop, exactly as the tool executor does: the
    row is there and marked running before the drive coroutine gets its first
    slice, which is what makes the swarm visible WHILE it runs."""
    from server.workflows import manager

    async def go():
        run_id = manager.start_run(_NOOP, session_id="s1", name="nightly-review")
        tasks = _workflow_tasks("s1")
        assert len(tasks) == 1
        assert tasks[0]["status"] == "running"
        assert "nightly-review" in tasks[0]["title"]
        assert (tasks[0]["meta"] or {}).get("run_id") == run_id
        await _await_done(run_id)

    asyncio.run(go())


def test_finished_run_closes_its_task_and_reaches_the_model():
    """The whole point of the mirror: completion_inject picks the finished run
    up on the next turn, so the model learns the outcome without polling."""
    from server.agents import completion_inject
    from server.workflows import manager

    async def go():
        run_id = manager.start_run(_NOOP, session_id="s1", name="noop")
        run = await _await_done(run_id)
        assert run["status"] == "done"
        return run_id

    asyncio.run(go())

    tasks = _workflow_tasks("s1")
    assert len(tasks) == 1
    assert tasks[0]["status"] == "completed"
    # The result the script returned is what the card and the model read.
    assert '"ok": 1' in (tasks[0]["result_text"] or "")

    pending = completion_inject.pending_completions("s1")
    assert [t["task_id"] for t in pending] == [tasks[0]["task_id"]]


def test_failed_run_closes_its_task_as_failed():
    from server.workflows import manager

    boom = (
        "export const meta = { name: 'boom', description: 'd', phases: [] }\n"
        "throw new Error('kaboom')\n"
    )

    async def go():
        run_id = manager.start_run(boom, session_id="s1", name="boom")
        run = await _await_done(run_id)
        assert run["status"] == "failed"

    asyncio.run(go())

    tasks = _workflow_tasks("s1")
    assert tasks[0]["status"] == "failed"
    assert "kaboom" in (tasks[0]["result_text"] or "")


def test_registry_failure_never_stops_the_run(monkeypatch):
    """Mirroring is bookkeeping. If it throws, the workflow still runs."""
    from server.workflows import manager

    def boom(*a, **kw):
        raise RuntimeError("registry down")

    monkeypatch.setattr("server.tasks.registry.create_task", boom)

    async def go():
        run_id = manager.start_run(_NOOP, session_id="s1", name="noop")
        return await _await_done(run_id)

    run = asyncio.run(go())
    assert run["status"] == "done"
    assert not _workflow_tasks("s1")
