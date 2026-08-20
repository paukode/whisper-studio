"""Answer-sources persistence and endpoint.

Each index-grounded chat turn persists the passages behind the answer
(server/infrastructure/grounding_store.py), keyed by the grounding id the
``grounding`` SSE event carries; the chat chip resolves it back via
GET /api/sessions/{session_id}/grounding/{grounding_id}. Rows are
session-scoped, capped per session, and die with the session.
"""

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure import grounding_store
from server.infrastructure.sessions import _delete_session_sync, _ensure_db


def _sources() -> list[dict]:
    return [
        {
            "path": "docs/a.md",
            "abs": "/ws/docs/a.md",
            "ws": "/ws",
            "start_line": 1,
            "end_line": 5,
            "kind": "semantic",
            "entities": [],
            "snippet": "the answer came from here",
        },
        {
            "path": "docs/b.md",
            "abs": "/ws/docs/b.md",
            "ws": "/ws",
            "start_line": 10,
            "end_line": 20,
            "kind": "related",
            "entities": ["PaymentService"],
            "snippet": "linked through a shared entity",
        },
    ]


def test_save_and_get_roundtrip():
    gid = grounding_store.save_grounding("sess-a", _sources())
    assert grounding_store.get_grounding("sess-a", gid) == _sources()
    # Session-scoped lookup: another session can't read this row, and an
    # unknown id is a plain miss, not an error.
    assert grounding_store.get_grounding("sess-b", gid) is None
    assert grounding_store.get_grounding("sess-a", "nope") is None


def test_per_session_cap_trims_oldest(monkeypatch):
    monkeypatch.setattr(grounding_store, "MAX_PER_SESSION", 3)
    gids = [grounding_store.save_grounding("sess-cap", [{"i": i}]) for i in range(5)]
    # The two oldest rows were trimmed; the newest three survive. Another
    # session's rows are untouched by the cap.
    other = grounding_store.save_grounding("sess-other", [{"i": 99}])
    assert grounding_store.get_grounding("sess-cap", gids[0]) is None
    assert grounding_store.get_grounding("sess-cap", gids[1]) is None
    for gid in gids[2:]:
        assert grounding_store.get_grounding("sess-cap", gid) is not None
    assert grounding_store.get_grounding("sess-other", other) is not None


def test_endpoint_returns_sources_and_404s():
    from server.infrastructure import sessions

    app = FastAPI()
    app.include_router(sessions.router)
    client = TestClient(app)

    gid = grounding_store.save_grounding("sess-http", _sources())
    resp = client.get(f"/api/sessions/sess-http/grounding/{gid}")
    assert resp.status_code == 200
    assert resp.json() == {"id": gid, "sources": _sources()}

    # Wrong session and unknown id both 404 (the chip degrades to counts-only).
    assert client.get(f"/api/sessions/other/grounding/{gid}").status_code == 404
    assert client.get("/api/sessions/sess-http/grounding/missing").status_code == 404


def test_session_delete_cascades_grounding():
    _ensure_db()
    sid = f"grounding-{uuid.uuid4()}"
    gid = grounding_store.save_grounding(sid, _sources())
    assert grounding_store.get_grounding(sid, gid) is not None

    _delete_session_sync(sid)
    assert grounding_store.get_grounding(sid, gid) is None
