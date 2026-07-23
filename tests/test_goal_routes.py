"""WS-E goal HTTP API: GET/POST/DELETE /api/sessions/{id}/goal."""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    from server.infrastructure import sessions

    storage = tmp_path / "storage"
    os.makedirs(storage, exist_ok=True)
    monkeypatch.setattr(sessions, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(sessions, "DB_PATH", str(storage / "sessions.db"))
    sessions._ensure_db()
    with sessions._get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES ('s1','t','2026','2026')"
        )
    from server.goals.routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_goal_crud(client):
    # Empty to start.
    r = client.get("/api/sessions/s1/goal")
    assert r.status_code == 200
    assert r.json()["goal"] == ""
    assert r.json()["state"]["active"] is False

    # Set.
    r = client.post("/api/sessions/s1/goal", json={"goal": "make the tests pass"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    got = client.get("/api/sessions/s1/goal").json()
    assert got["goal"] == "make the tests pass"
    assert got["state"]["active"] is True
    assert got["state"]["set_at"]  # timestamp recorded

    # Clear.
    assert client.delete("/api/sessions/s1/goal").status_code == 200
    assert client.get("/api/sessions/s1/goal").json()["goal"] == ""


def test_empty_goal_rejected(client):
    assert client.post("/api/sessions/s1/goal", json={"goal": "   "}).status_code == 400
