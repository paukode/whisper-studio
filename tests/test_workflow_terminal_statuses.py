"""Every way a workflow run can end lands in manager.TERMINAL_STATUSES, and
every reader that asks "has this run ended" answers from that one set.

The set used to be spelled out in four places, with a 'completed' alias the
runtime never produces.
"""

from __future__ import annotations

import pytest

from server.workflows import manager, tools
from server.workflows.runtime import WorkflowRun


@pytest.fixture(autouse=True)
def _clean_live():
    manager._live.clear()
    manager._task_ids.clear()
    yield
    manager._live.clear()
    manager._task_ids.clear()


def _seed_running(run_id: str) -> str:
    with manager._conn() as conn:
        manager._ensure_table(conn)
        conn.execute(
            "INSERT INTO workflow_runs (run_id, name, session_id, status, started_at) "
            "VALUES (?,?,?,?,?)",
            (run_id, "wf", "s1", "running", manager._now()),
        )
    return run_id


@pytest.mark.parametrize("ending", ["done", "failed"])
def test_runtime_outcomes_are_terminal(ending, monkeypatch):
    monkeypatch.setattr(manager, "_publish", lambda *a, **k: None)
    run = WorkflowRun(_seed_running(f"r-{ending}"), "", session_id="s1")
    manager._finalize(run, run._result(ending))
    status = manager.get_run(run.run_id)["status"]
    assert status == ending
    assert status in manager.TERMINAL_STATUSES


def test_stop_and_reconcile_endings_are_terminal():
    import asyncio

    class _Live:
        async def cancel(self):
            return None

    stopped = _seed_running("r-stop")
    manager._live[stopped] = _Live()
    assert asyncio.run(manager.stop_run(stopped)) is True
    assert manager.get_run(stopped)["status"] in manager.TERMINAL_STATUSES

    stale = _seed_running("r-stale")
    assert manager.reconcile_stale() >= 1
    assert manager.get_run(stale)["status"] in manager.TERMINAL_STATUSES
    assert "running" not in manager.TERMINAL_STATUSES


@pytest.mark.parametrize("status", sorted(manager.TERMINAL_STATUSES | {"running"}))
def test_wait_view_finishes_exactly_on_terminal(status, monkeypatch):
    monkeypatch.setattr(
        manager, "get_run", lambda run_id, **k: {"run_id": run_id, "status": status}
    )
    view = tools._wait_view("r1")
    assert view["finished"] is (status in manager.TERMINAL_STATUSES)
