"""Session pin/archive flags, branch, and open-workspace endpoints."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure import sessions as sessions_mod
from server.infrastructure.sessions import _ensure_db
from server.infrastructure.sessions import router as sessions_router
from server.migrations.runner import run_migrations


def _client():
    app = FastAPI()
    app.include_router(sessions_router)
    _ensure_db()
    run_migrations()  # pinned/archived columns come from migration 005
    return TestClient(app)


def _seed(client: TestClient, sid: str, **extra) -> None:
    r = client.put(
        f"/api/sessions/{sid}",
        json={
            "id": sid,
            "title": f"flags {sid}",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "segments": [],
            "chatHistory": [],
            "speakerNames": {},
            **extra,
        },
    )
    assert r.status_code == 200, r.text


def _summary(client: TestClient, sid: str) -> dict:
    rows = client.get("/api/sessions").json()
    return next(s for s in rows if s["id"] == sid)


def test_flags_roundtrip_in_list_summary():
    client = _client()
    sid = "flags-roundtrip"
    _seed(client, sid)
    try:
        assert _summary(client, sid)["pinned"] is False
        assert _summary(client, sid)["archived"] is False

        r = client.patch(f"/api/sessions/{sid}/flags", json={"pinned": True})
        assert r.status_code == 200
        s = _summary(client, sid)
        assert s["pinned"] is True and s["archived"] is False

        # Partial update must not clobber the other flag.
        r = client.patch(f"/api/sessions/{sid}/flags", json={"archived": True})
        assert r.status_code == 200
        s = _summary(client, sid)
        assert s["pinned"] is True and s["archived"] is True

        r = client.patch(f"/api/sessions/{sid}/flags", json={"pinned": False, "archived": False})
        assert r.status_code == 200
        s = _summary(client, sid)
        assert s["pinned"] is False and s["archived"] is False
    finally:
        client.delete(f"/api/sessions/{sid}")


def test_flags_rejects_empty_body():
    client = _client()
    assert client.patch("/api/sessions/whatever/flags", json={}).status_code == 400


def test_branch_copies_content_into_new_session():
    client = _client()
    sid = "flags-branch-src"
    new_id = None
    _seed(
        client,
        sid,
        segments=[
            {"id": "s1", "speaker": "Speaker 1", "text": "hi", "timestamp": 1, "edited": False}
        ],
    )
    try:
        r = client.post(f"/api/sessions/{sid}/branch")
        assert r.status_code == 200, r.text
        body = r.json()
        new_id = body["new_session_id"]
        assert new_id != sid
        assert "(branch" in body["name"]
        s = _summary(client, new_id)
        assert s["segmentCount"] == 1
        # Branches never inherit flags.
        assert s["pinned"] is False and s["archived"] is False
    finally:
        client.delete(f"/api/sessions/{sid}")
        if new_id:
            client.delete(f"/api/sessions/{new_id}")


def test_open_workspace_validation(tmp_path, monkeypatch):
    client = _client()
    sid = "flags-open-ws"
    _seed(client, sid)
    try:
        # Unknown app and unknown session.
        r = client.post(f"/api/sessions/{sid}/open-workspace", json={"app": "emacs"})
        assert r.status_code == 400
        r = client.post("/api/sessions/nope/open-workspace", json={"app": "finder"})
        assert r.status_code == 404
        # Session without a workspace folder.
        r = client.post(f"/api/sessions/{sid}/open-workspace", json={"app": "finder"})
        assert r.status_code == 400

        # With a real folder: launch is attempted with the mapped command.
        client.put(
            f"/api/sessions/{sid}",
            json={
                "id": sid,
                "title": "with ws",
                "workspacePath": str(tmp_path),
                "createdAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
                "segments": [],
                "chatHistory": [],
                "speakerNames": {},
            },
        )
        calls = []

        class _Result:
            returncode = 0

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _Result()

        import subprocess

        monkeypatch.setattr(subprocess, "run", fake_run)
        r = client.post(f"/api/sessions/{sid}/open-workspace", json={"app": "kiro"})
        assert r.status_code == 200, r.text
        assert len(calls) == 1
        assert str(tmp_path) in calls[0]
    finally:
        client.delete(f"/api/sessions/{sid}")
