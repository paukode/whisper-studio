"""Persistent notification log: choke-point recording, list, read, GC."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

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


def test_record_once_skips_a_notice_already_recorded_even_when_hidden():
    first = notif.record_notification_once(
        source="config", title="Old", message="m", status="warning"
    )
    assert first["deduped"] is False
    again = notif.record_notification_once(
        source="config", title="Old", message="m", status="warning"
    )
    assert again == {"id": first["id"], "deduped": True}
    [row] = notif.list_notifications()
    assert row["count"] == 1 and row["status"] == "warning"

    # Hidden in the bell, it is still "already shown": nothing comes back.
    notif.hide([first["id"]])
    assert notif.record_notification_once(source="config", title="Old", message="m")["deduped"]
    assert notif.list_notifications() == []

    # A different message is a different notice; an empty one is refused.
    assert not notif.record_notification_once(source="config", title="Old", message="m2")["deduped"]
    assert notif.record_notification_once(source="config", message="") is None


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
    old = (datetime.now(UTC) - timedelta(days=45)).isoformat().replace("+00:00", "Z")
    with notif._get_conn() as conn:
        conn.execute("UPDATE notifications SET created_at=?", (old,))
    notif.record_notification(message="fresh", source="chat")
    assert notif.gc_old() == 1
    remaining = notif.list_notifications()
    assert [r["message"] for r in remaining] == ["fresh"]


def test_gc_keeps_the_rows_that_remember_a_notice_was_shown():
    for source in notif.ONCE_SOURCES:
        notif.record_notification_once(source=source, title="Old", message="m")
    notif.record_notification(message="old", source="chat")
    old = (datetime.now(UTC) - timedelta(days=45)).isoformat().replace("+00:00", "Z")
    with notif._get_conn() as conn:
        conn.execute("UPDATE notifications SET created_at=?", (old,))

    assert notif.gc_old() == 1
    assert sorted(r["source"] for r in notif.list_notifications()) == sorted(notif.ONCE_SOURCES)
    # So the notice is still "already shown" after the retention window.
    assert notif.record_notification_once(source="config", title="Old", message="m")["deduped"]


class _Log:
    def __init__(self):
        self.lines = []

    def warning(self, fmt, *args):
        self.lines.append(fmt % args)


def test_old_format_notice_logs_once_per_process_and_shows_once(monkeypatch):
    log = _Log()
    monkeypatch.setattr(notif, "log", log)
    monkeypatch.setattr(notif, "_old_format_seen", set())
    notif.old_format_notice(title="Old hooks", message="m")
    notif.old_format_notice(title="Old hooks", message="m")
    assert log.lines == ["Old hooks: m"]
    [row] = notif.list_notifications()
    assert row["source"] == notif.OLD_FORMAT_SOURCE == "old-format"
    assert row["status"] == "warning" and row["count"] == 1
    assert notif.OLD_FORMAT_SOURCE in notif.ONCE_SOURCES

    # The next start (a new process) logs it again but does not show it again,
    # even after the user cleared it.
    notif.clear_all()
    monkeypatch.setattr(notif, "_old_format_seen", set())
    notif.old_format_notice(title="Old hooks", message="m")
    assert log.lines == ["Old hooks: m", "Old hooks: m"]
    assert notif.list_notifications() == []


def test_old_format_notice_never_raises(monkeypatch):
    log = _Log()
    monkeypatch.setattr(notif, "log", log)
    monkeypatch.setattr(notif, "_old_format_seen", set())

    def broken(**kw):
        raise RuntimeError("no database")

    monkeypatch.setattr(notif, "record_notification_once", broken)
    notif.old_format_notice(title="T", message="m")
    assert log.lines[0] == "T: m"
    assert "no database" in log.lines[1]


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
    assert output == "Notification sent to user: cron says hi"
    assert side_effects[0]["notify_user"]["status"] == "success"
    rows = notif.list_notifications()
    assert len(rows) == 1
    assert rows[0]["source"] == "cron"
    assert rows[0]["session_id"] == "sess-cron"
    assert rows[0]["title"] == "Done"


def test_notify_user_tool_result_carries_delivered_message():
    """The tool_result must carry the actual delivered text, not a generic
    ack — server.goals.tail.render_tail renders tool_result content into the
    transcript tail both the interactive completion gate and cron's own
    verify-and-continue judge, so a plain "sent." previously hid a genuinely
    delivered report from both evaluators."""
    from server.tool_router import route_tool

    async def _run(message):
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as ex:
            return await route_tool(
                "notify_user",
                {"message": message},
                loop=loop,
                executor=ex,
                transcript="",
                attachments=None,
                session_id="sess-x",
                model_id="m",
                tool_use_id="t1",
            )

    output, _ = asyncio.run(_run("all endpoints healthy, see https://example.com/status"))
    assert (
        output == "Notification sent to user: all endpoints healthy, see https://example.com/status"
    )

    # An empty message keeps the plain ack (nothing to surface).
    output_empty, _ = asyncio.run(_run(""))
    assert output_empty == "Notification sent to user."


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
        (datetime.now(UTC) - timedelta(seconds=notif.DEDUPE_WINDOW_SECONDS + 1))
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


def test_legacy_check_constraint_fails_gracefully():
    """A pre-012 DB (source CHECK) can only exist if migration 012 failed;
    the write is dropped with a warning instead of being retagged."""
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
    assert r is None
    assert notif.list_notifications() == []


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


def test_a_database_error_reaches_the_bell_instead_of_an_empty_list(monkeypatch):
    import contextlib
    import sqlite3

    import pytest

    from server import notifications as N

    N._ensure_table()

    @contextlib.contextmanager
    def _locked():
        raise sqlite3.OperationalError("database is locked")
        yield

    monkeypatch.setattr(N, "_get_conn", _locked)
    for call in (N.list_notifications, N.unread_count, N.clear_all, N.gc_old):
        with pytest.raises(sqlite3.OperationalError):
            call()
