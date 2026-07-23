"""POST /api/sessions/bulk-delete removes every listed session (and only
those), running the same cascade as single delete, and rejects bad input."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure.sessions import _ensure_db
from server.infrastructure.sessions import router as sessions_router


def _client():
    app = FastAPI()
    app.include_router(sessions_router)
    _ensure_db()
    return TestClient(app)


def _seed(client: TestClient, sid: str) -> None:
    r = client.put(
        f"/api/sessions/{sid}",
        json={
            "id": sid,
            "title": f"bulk {sid}",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "segments": [],
            "chatHistory": [],
            "speakerNames": {},
        },
    )
    assert r.status_code == 200, r.text


def test_bulk_delete_removes_only_listed_sessions():
    client = _client()
    ids = [f"bulk-del-{i}" for i in range(3)]
    keeper = "bulk-del-keeper"
    for sid in [*ids, keeper]:
        _seed(client, sid)

    r = client.post("/api/sessions/bulk-delete", json={"ids": ids})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "deleted": 3}

    for sid in ids:
        assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert client.get(f"/api/sessions/{keeper}").status_code == 200

    client.delete(f"/api/sessions/{keeper}")  # cleanup


def test_bulk_delete_dedupes_ids():
    client = _client()
    sid = "bulk-del-dup"
    _seed(client, sid)
    r = client.post("/api/sessions/bulk-delete", json={"ids": [sid, sid, sid]})
    assert r.status_code == 200
    assert r.json()["deleted"] == 1


def test_bulk_delete_rejects_bad_payloads():
    client = _client()
    assert client.post("/api/sessions/bulk-delete", json={"ids": "not-a-list"}).status_code == 400
    assert client.post("/api/sessions/bulk-delete", json={"ids": [1, 2]}).status_code == 400
    assert client.post("/api/sessions/bulk-delete", json={}).status_code == 400
    too_many = {"ids": [f"x{i}" for i in range(501)]}
    assert client.post("/api/sessions/bulk-delete", json=too_many).status_code == 400
