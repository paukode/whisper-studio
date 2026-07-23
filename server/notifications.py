"""Persistent notification log behind the header bell.

Single choke-point write: the ``notify_user`` branch in server/tool_router.py
calls :func:`record_notification` best-effort, so chat turns, agent runs, and
cron jobs (which all dispatch through ``route_tool``) land in one log with an
``origin`` tag. The ephemeral toast stays; this is the durable record a user
who was away from the screen can catch up from.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import Response

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
DB_PATH = os.path.join(STORAGE_DIR, "sessions.db")

RETENTION_DAYS = 30
_VALID_SOURCES = ("chat", "agent", "cron")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def _get_conn():
    os.makedirs(STORAGE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.DatabaseError:
        pass
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_table() -> None:
    """Defensive net alongside migration 008 (tests, fresh boots)."""
    try:
        with _get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT,
                    source      TEXT NOT NULL CHECK (source IN ('chat', 'agent', 'cron')),
                    title       TEXT,
                    message     TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'normal',
                    created_at  TEXT NOT NULL,
                    read_at     TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notifications_unread "
                "ON notifications(read_at, created_at DESC)"
            )
    except sqlite3.DatabaseError as e:
        log.warning("notifications: ensure table failed: %s", e)


def record_notification(
    *,
    session_id: str = "",
    source: str = "chat",
    title: str = "",
    message: str,
    status: str = "normal",
) -> None:
    """Best-effort durable write; never raises into the tool path."""
    if not message:
        return
    if source not in _VALID_SOURCES:
        source = "chat"
    _ensure_table()
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO notifications (session_id, source, title, message, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id or "",
                    source,
                    (title or "")[:200],
                    message[:4000],
                    status or "normal",
                    _utc_now_iso(),
                ),
            )
    except sqlite3.DatabaseError as e:
        log.warning("notifications: record failed: %s", e)


def list_notifications(*, limit: int = 50, unread_only: bool = False) -> list[dict]:
    _ensure_table()
    where = "WHERE read_at IS NULL" if unread_only else ""
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM notifications {where} ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.DatabaseError as e:
        log.warning("notifications: list failed: %s", e)
        return []


def unread_count() -> int:
    _ensure_table()
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE read_at IS NULL"
            ).fetchone()
        return int(row["n"]) if row else 0
    except sqlite3.DatabaseError:
        return 0


def mark_read(ids: list[int] | None = None) -> int:
    """Mark specific ids read, or ALL unread when ids is None/empty."""
    _ensure_table()
    now = _utc_now_iso()
    try:
        with _get_conn() as conn:
            if ids:
                placeholders = ",".join("?" for _ in ids)
                cur = conn.execute(
                    f"UPDATE notifications SET read_at=? WHERE id IN ({placeholders}) "
                    "AND read_at IS NULL",
                    (now, *ids),
                )
            else:
                cur = conn.execute(
                    "UPDATE notifications SET read_at=? WHERE read_at IS NULL", (now,)
                )
            return cur.rowcount or 0
    except sqlite3.DatabaseError as e:
        log.warning("notifications: mark_read failed: %s", e)
        return 0


def gc_old(days: int = RETENTION_DAYS) -> int:
    """Drop notifications older than the retention window (called on boot)."""
    _ensure_table()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    try:
        with _get_conn() as conn:
            cur = conn.execute("DELETE FROM notifications WHERE created_at < ?", (cutoff,))
            return cur.rowcount or 0
    except sqlite3.DatabaseError:
        return 0


# ── HTTP surface ─────────────────────────────────────────────────────────────


@router.get("")
async def api_list(limit: int = 50, unread_only: bool = False):
    return {"notifications": list_notifications(limit=limit, unread_only=unread_only)}


@router.get("/unread-count")
async def api_unread_count():
    return {"unread": unread_count()}


@router.post("/read")
async def api_mark_read(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    ids = body.get("ids")
    if ids is not None and not isinstance(ids, list):
        return Response(
            content='{"error": "ids must be a list of integers"}',
            status_code=400,
            media_type="application/json",
        )
    try:
        id_list = [int(i) for i in ids] if ids else None
    except (ValueError, TypeError):
        return Response(
            content='{"error": "ids must be a list of integers"}',
            status_code=400,
            media_type="application/json",
        )
    marked = mark_read(id_list)
    return {"marked": marked}
