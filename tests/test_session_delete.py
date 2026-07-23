"""Deleting a session must cascade — the parent row AND every child row
keyed by session_id are removed, not just the ``sessions`` entry.

Earlier ``DELETE /api/sessions/{id}`` only ran ``DELETE FROM sessions``,
leaving ``session_costs`` and ``tasks`` rows orphaned in sessions.db.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import tasks_tracker
from server.costs import tracker as costs
from server.infrastructure.sessions import _ensure_db
from server.infrastructure.sessions import router as sessions_router
from server.migrations.runner import run_migrations


def _client():
    app = FastAPI()
    app.include_router(sessions_router)
    _ensure_db()
    # _ensure_db() creates the base schema; migrations add later columns
    # (e.g. sessions.workspace_path from 002). The app runs these at lifespan
    # startup, which the test bypasses — so apply them here too. Idempotent.
    run_migrations()
    return TestClient(app)


def test_delete_cascades_to_costs_and_tasks():
    client = _client()
    sid = "test-delete-cascade-1"

    # Parent session row.
    r = client.put(
        f"/api/sessions/{sid}",
        json={
            "id": sid,
            "title": "to delete",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "segments": [],
            "chatHistory": [],
            "speakerNames": {},
        },
    )
    assert r.status_code == 200, r.text

    # Child rows in the two related tables.
    costs.record_turn(
        sid, turn_number=1, model="claude", input_tokens=10, output_tokens=20, cost_usd=0.01
    )
    tasks_tracker.create_task(sid, subject="a task")

    # Sanity: everything is present before the delete.
    assert client.get(f"/api/sessions/{sid}").status_code == 200
    assert len(costs.get_session_costs(sid)) == 1
    assert len(tasks_tracker.get_session_tasks(sid)) == 1

    # Delete cascades.
    r = client.delete(f"/api/sessions/{sid}")
    assert r.status_code == 200, r.text

    # Parent gone, and NO orphaned child rows survive.
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert costs.get_session_costs(sid) == []
    assert tasks_tracker.get_session_tasks(sid) == []
