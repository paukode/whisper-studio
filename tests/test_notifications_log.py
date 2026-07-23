"""Persistent notification log: choke-point recording, list, read, GC."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.notifications as notif


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(notif, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(notif, "DB_PATH", str(tmp_path / "sessions.db"))
    yield


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(notif.router)
    return TestClient(app)


def test_record_and_list_roundtrip():
    notif.record_notification(
        session_id="s1", source="cron", title="Nightly", message="report ready", status="success"
    )
    rows = notif.list_notifications()
    assert len(rows) == 1
    assert rows[0]["source"] == "cron"
    assert rows[0]["title"] == "Nightly"
    assert rows[0]["status"] == "success"
    assert rows[0]["read_at"] is None


def test_empty_message_and_bad_source_handled():
    notif.record_notification(message="", source="cron")
    assert notif.list_notifications() == []
    notif.record_notification(message="x", source="martian")
    assert notif.list_notifications()[0]["source"] == "chat"  # coerced


def test_unread_count_and_mark_read():
    for i in range(3):
        notif.record_notification(message=f"n{i}", source="chat")
    assert notif.unread_count() == 3
    first_id = notif.list_notifications()[-1]["id"]
    assert notif.mark_read([first_id]) == 1
    assert notif.unread_count() == 2
    assert notif.mark_read(None) == 2  # mark-all
    assert notif.unread_count() == 0


def test_gc_drops_old_rows():
    notif.record_notification(message="old", source="chat")
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat().replace("+00:00", "Z")
    with notif._get_conn() as conn:
        conn.execute("UPDATE notifications SET created_at=?", (old,))
    notif.record_notification(message="fresh", source="chat")
    assert notif.gc_old() == 1
    remaining = notif.list_notifications()
    assert [r["message"] for r in remaining] == ["fresh"]


def test_http_surface(client):
    notif.record_notification(message="hello", source="agent", status="warning")
    data = client.get("/api/notifications").json()
    assert data["notifications"][0]["message"] == "hello"
    assert client.get("/api/notifications/unread-count").json() == {"unread": 1}
    r = client.post("/api/notifications/read", json={})
    assert r.json() == {"marked": 1}
    assert client.get("/api/notifications/unread-count").json() == {"unread": 0}
    assert client.post("/api/notifications/read", json={"ids": "nope"}).status_code == 400


def test_route_tool_choke_point_records_with_origin():
    """notify_user through route_tool must land in the log with its origin."""
    from server.tool_router import route_tool

    async def _run():
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as ex:
            return await route_tool(
                "notify_user",
                {"message": "cron says hi", "status": "success", "title": "Done"},
                loop=loop,
                executor=ex,
                transcript="",
                attachments=None,
                session_id="sess-cron",
                model_id="m",
                tool_use_id="t1",
                origin="cron",
            )

    output, side_effects = asyncio.run(_run())
    assert output == "Notification sent to user."
    assert side_effects[0]["notify_user"]["status"] == "success"
    rows = notif.list_notifications()
    assert len(rows) == 1
    assert rows[0]["source"] == "cron"
    assert rows[0]["session_id"] == "sess-cron"
    assert rows[0]["title"] == "Done"


def test_mark_read_rejects_non_numeric_ids(client):
    r = client.post("/api/notifications/read", json={"ids": ["abc"]})
    assert r.status_code == 400
