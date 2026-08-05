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
    assert notif.record_notification(message="", source="cron") is None
    assert notif.list_notifications() == []
    notif.record_notification(message="x", source="Model Download!!")
    assert notif.list_notifications()[0]["source"] == "model-download"  # slugified
    notif.record_notification(message="y", source="???")
    assert notif.list_notifications()[0]["source"] == "app"  # garbage → app


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


def test_hide_excludes_from_list_and_unread():
    for i in range(3):
        notif.record_notification(message=f"n{i}", source="chat")
    target = notif.list_notifications()[0]["id"]
    assert notif.hide([target]) == 1
    remaining = notif.list_notifications()
    assert len(remaining) == 2
    assert target not in [r["id"] for r in remaining]
    # A hidden row is also read, so it never haunts the badge.
    assert notif.unread_count() == 2
    # Hiding again is a no-op.
    assert notif.hide([target]) == 0


def test_clear_all_hides_everything_but_keeps_rows():
    for i in range(3):
        notif.record_notification(message=f"n{i}", source="agent")
    assert notif.clear_all() == 3
    assert notif.list_notifications() == []
    assert notif.unread_count() == 0
    with notif._get_conn() as conn:
        kept = conn.execute("SELECT COUNT(*) AS n FROM notifications").fetchone()["n"]
    assert kept == 3  # soft-hide: rows stay for the 30-day GC


def test_dedupe_merges_identical_within_window():
    r1 = notif.record_notification(message="boom", title="Fail", source="app", status="error")
    notif.mark_read([r1["id"]])
    r2 = notif.record_notification(message="boom", title="Fail", source="app", status="error")
    assert r1["deduped"] is False
    assert r2 == {"id": r1["id"], "deduped": True}
    rows = notif.list_notifications()
    assert len(rows) == 1
    assert rows[0]["count"] == 2
    # A repeat is a new occurrence: the merged row goes unread again.
    assert rows[0]["read_at"] is None
    assert notif.unread_count() == 1
    # A different body does NOT merge.
    notif.record_notification(message="other", title="Fail", source="app", status="error")
    assert len(notif.list_notifications()) == 2


def test_dedupe_window_expires():
    notif.record_notification(message="boom", source="app", status="error")
    stale = (
        (datetime.now(timezone.utc) - timedelta(seconds=notif.DEDUPE_WINDOW_SECONDS + 1))
        .isoformat()
        .replace("+00:00", "Z")
    )
    with notif._get_conn() as conn:
        conn.execute("UPDATE notifications SET created_at=?", (stale,))
    r = notif.record_notification(message="boom", source="app", status="error")
    assert r["deduped"] is False
    rows = notif.list_notifications()
    assert len(rows) == 2
    assert all(row["count"] == 1 for row in rows)


def test_post_endpoint_records_app_notification(client):
    r = client.post(
        "/api/notifications",
        json={
            "message": "Download failed",
            "title": "Gemma 12B",
            "source": "model-download",
            "status": "error",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["deduped"] is False and isinstance(data["id"], int)
    row = client.get("/api/notifications").json()["notifications"][0]
    assert row["source"] == "model-download"
    assert row["title"] == "Gemma 12B"
    assert row["status"] == "error"
    assert row["count"] == 1
    # Same payload again inside the window → merged, repeat count bumped.
    r2 = client.post(
        "/api/notifications",
        json={
            "message": "Download failed",
            "title": "Gemma 12B",
            "source": "model-download",
            "status": "error",
        },
    )
    assert r2.json() == {"ok": True, "id": data["id"], "deduped": True}
    rows = client.get("/api/notifications").json()["notifications"]
    assert len(rows) == 1
    assert rows[0]["count"] == 2


def test_post_endpoint_defaults_and_validation(client):
    assert client.post("/api/notifications", json={}).status_code == 400
    assert client.post("/api/notifications", json={"message": "   "}).status_code == 400
    assert client.post("/api/notifications", json={"message": 42}).status_code == 400
    r = client.post("/api/notifications", json={"message": "hi", "status": "martian"})
    assert r.status_code == 200
    row = client.get("/api/notifications").json()["notifications"][0]
    assert row["source"] == "app"  # default for frontend posts
    assert row["status"] == "normal"  # unknown status coerced


def test_legacy_check_constraint_falls_back_to_chat():
    """A pre-012 DB (source CHECK) still gets the row, tagged chat."""
    with notif._get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE notifications (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT,
                source      TEXT NOT NULL CHECK (source IN ('chat', 'agent', 'cron')),
                title       TEXT,
                message     TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'normal',
                created_at  TEXT NOT NULL,
                read_at     TEXT,
                hidden_at   TEXT,
                count       INTEGER NOT NULL DEFAULT 1
            )
            """
        )
    r = notif.record_notification(message="hello", source="model-download")
    assert r is not None and r["deduped"] is False
    assert notif.list_notifications()[0]["source"] == "chat"


def test_hide_and_clear_http_surface(client):
    notif.record_notification(message="a", source="chat")
    notif.record_notification(message="b", source="chat")
    rows = client.get("/api/notifications").json()["notifications"]
    r = client.post("/api/notifications/hide", json={"ids": [rows[0]["id"]]})
    assert r.json() == {"hidden": 1}
    assert client.post("/api/notifications/hide", json={}).status_code == 400
    assert client.post("/api/notifications/hide", json={"ids": []}).status_code == 400
    assert client.post("/api/notifications/hide", json={"ids": ["x"]}).status_code == 400
    assert client.post("/api/notifications/clear", json={}).json() == {"cleared": 1}
    assert client.get("/api/notifications").json()["notifications"] == []
