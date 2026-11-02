"""Persistent notification log.

Writers: ``notify_user`` (server/tool_router.py, for chat turns, agent runs
and cron jobs alike), agent wake reports (server/agents/wake.py), background
task results (server/tasks/events.py), unanswered mid-turn messages
(server/chat/engine/midturn_inbox.py), startup notices about old settings,
and frontend toasts, which POST themselves here (``POST /api/notifications``,
wired centrally in the uiStore's addToast) so the bell keeps a reviewable
history after the 5-second toast is gone. The record_* writers are
best-effort (a failed write is logged, never raised into the caller). Each row carries an ``source`` slug
("chat", "cron", "app", "model-download", ...). Identical rows fired in a
tight loop collapse into one entry with a repeat ``count`` (see
:data:`DEDUPE_WINDOW_SECONDS`). Surfaced by the header bell
(src/components/layout/NotificationsBell.tsx) over ``GET /api/notifications``.
"""

import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.infrastructure.paths import storage_root

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

STORAGE_DIR = storage_root()
DB_PATH = os.path.join(STORAGE_DIR, "sessions.db")

RETENTION_DAYS = 30
# Source of the notices about old-format files the app no longer reads (cron
# jobs, hooks, memory folders); see old_format_notice.
OLD_FORMAT_SOURCE = "old-format"
# Sources whose rows record_notification_once relies on to remember that a
# notice was shown. gc_old keeps them: dropping one would show the notice again.
# They are a handful of notices about old settings (server/main.py) and old
# file formats (old_format_notice).
ONCE_SOURCES = ("config", OLD_FORMAT_SOURCE)
# Identical (source, title, message, status) rows arriving within this many
# seconds of the previous occurrence merge into it: count += 1 and the row's
# created_at slides forward. A steady drip of the same failure therefore
# stays ONE bell entry while it keeps firing.
DEDUPE_WINDOW_SECONDS = 5.0
_VALID_STATUSES = ("normal", "info", "success", "warning", "error")


def _slug_source(source: str) -> str:
    """Normalize a source tag to a lowercase slug; empty/garbage → "app"."""
    s = re.sub(r"[^a-z0-9_-]+", "-", (source or "").strip().lower()).strip("-")
    return s[:32] or "app"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@contextmanager
def _get_conn():
    os.makedirs(STORAGE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.DatabaseError as e:
        log.warning("notifications: failed to set sqlite pragmas: %s", e)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# The database path whose table is known to exist (checked once per path).
_table_ready_for: str | None = None


def _ensure_table() -> None:
    """Create the table and its indexes when missing, matching migrations
    008/010/012 (tests and a fresh database use it). Runs once per path."""
    global _table_ready_for
    if _table_ready_for == DB_PATH:
        return
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT,
                source      TEXT NOT NULL,
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_unread "
            "ON notifications(read_at, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_session "
            "ON notifications(session_id, created_at DESC)"
        )
    _table_ready_for = DB_PATH


def _insert_or_bump(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    source: str,
    title: str,
    message: str,
    status: str,
) -> dict:
    """Dedupe-aware write. Returns {"id": int, "deduped": bool}."""
    now = _utc_now_iso()
    cutoff = (
        (datetime.now(UTC) - timedelta(seconds=DEDUPE_WINDOW_SECONDS))
        .isoformat()
        .replace("+00:00", "Z")
    )
    row = conn.execute(
        "SELECT id FROM notifications WHERE hidden_at IS NULL "
        "AND source=? AND title=? AND message=? AND status=? AND created_at >= ? "
        "ORDER BY id DESC LIMIT 1",
        (source, title, message, status, cutoff),
    ).fetchone()
    if row:
        # A repeat is a NEW occurrence: slide created_at (so the row stays
        # current and the window keeps absorbing the burst) and clear read_at
        # so the badge re-lights if the user had already seen the older one.
        conn.execute(
            "UPDATE notifications SET count = count + 1, created_at = ?, read_at = NULL "
            "WHERE id = ?",
            (now, row["id"]),
        )
        return {"id": int(row["id"]), "deduped": True}
    cur = conn.execute(
        "INSERT INTO notifications (session_id, source, title, message, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, source, title, message, status, now),
    )
    return {"id": int(cur.lastrowid or 0), "deduped": False}


def record_notification(
    *,
    session_id: str = "",
    source: str = "chat",
    title: str = "",
    message: str,
    status: str = "normal",
) -> dict | None:
    """Best-effort durable write; never raises into the tool path.

    Returns {"id": ..., "deduped": ...} on success, None on failure.
    """
    if not message:
        return None
    source = _slug_source(source)
    fields = {
        "session_id": session_id or "",
        "title": (title or "")[:200],
        "message": message[:4000],
        "status": status if status in _VALID_STATUSES else "normal",
    }
    try:
        _ensure_table()
        with _get_conn() as conn:
            return _insert_or_bump(conn, source=source, **fields)
    except sqlite3.DatabaseError as e:
        log.warning("notifications: record failed: %s", e)
        return None


def record_notification_once(
    *,
    session_id: str = "",
    source: str = "app",
    title: str = "",
    message: str,
    status: str = "normal",
) -> dict | None:
    """Record a notice about something that stays true across restarts (an old
    setting the app ignores) the FIRST time only. When a row with the same
    source, title and message is already there, read, unread or hidden, nothing
    is written, so the bell does not repeat it on every start. The memory lives
    in this log, not in the user's files. Use a source in ONCE_SOURCES: gc_old
    keeps those rows past RETENTION_DAYS, so the notice never comes back.

    Returns {"id": ..., "deduped": ...} like record_notification, None on failure.
    """
    if not message:
        return None
    source = _slug_source(source)
    title = (title or "")[:200]
    message = message[:4000]
    try:
        _ensure_table()
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM notifications WHERE source=? AND title=? AND message=? "
                "ORDER BY id DESC LIMIT 1",
                (source, title, message),
            ).fetchone()
            if row:
                return {"id": int(row["id"]), "deduped": True}
            return _insert_or_bump(
                conn,
                session_id=session_id or "",
                source=source,
                title=title,
                message=message,
                status=status if status in _VALID_STATUSES else "normal",
            )
    except sqlite3.DatabaseError as e:
        log.warning("notifications: record failed: %s", e)
        return None


# Old-format notices this process has already handled, keyed by (title,
# message). The checks behind them run on every load (hooks on every tool
# call), so each notice is logged once per process and shown in the bell once.
_old_format_seen: set[tuple[str, str]] = set()


def old_format_notice(*, title: str, message: str) -> None:
    """Say that old-format data on disk is not used: a log warning once per
    process, and a bell warning the first time only (record_notification_once).
    Nothing on disk changes. Never raises: callers are on the tool path."""
    key = (title, message)
    if key in _old_format_seen:
        return
    _old_format_seen.add(key)
    log.warning("%s: %s", title, message)
    try:
        record_notification_once(
            source=OLD_FORMAT_SOURCE, title=title, message=message, status="warning"
        )
    except Exception as e:  # the log line above already carries the notice
        log.warning("Could not show the notice %r in the app: %s", title, e)


def list_notifications(*, limit: int = 50, unread_only: bool = False) -> list[dict]:
    """Visible (non-hidden) notifications, newest first."""
    _ensure_table()
    where = "WHERE hidden_at IS NULL"
    if unread_only:
        where += " AND read_at IS NULL"
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM notifications {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    return [dict(r) for r in rows]


def unread_count() -> int:
    _ensure_table()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE read_at IS NULL AND hidden_at IS NULL"
        ).fetchone()
    return int(row["n"]) if row else 0


def mark_read(ids: list[int] | None = None) -> int:
    """Mark specific ids read, or ALL unread when ids is None/empty."""
    _ensure_table()
    now = _utc_now_iso()
    with _get_conn() as conn:
        if ids:
            placeholders = ",".join("?" for _ in ids)
            cur = conn.execute(
                f"UPDATE notifications SET read_at=? WHERE id IN ({placeholders}) "
                "AND read_at IS NULL",
                (now, *ids),
            )
        else:
            cur = conn.execute("UPDATE notifications SET read_at=? WHERE read_at IS NULL", (now,))
        return cur.rowcount or 0


def hide(ids: list[int]) -> int:
    """Soft-hide specific notifications (a hidden row also counts as read)."""
    if not ids:
        return 0
    _ensure_table()
    now = _utc_now_iso()
    with _get_conn() as conn:
        placeholders = ",".join("?" for _ in ids)
        cur = conn.execute(
            f"UPDATE notifications SET hidden_at=?, read_at=COALESCE(read_at, ?) "
            f"WHERE id IN ({placeholders}) AND hidden_at IS NULL",
            (now, now, *ids),
        )
        return cur.rowcount or 0


def clear_all() -> int:
    """Soft-hide every visible notification (the bell's "Clear all")."""
    _ensure_table()
    now = _utc_now_iso()
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE notifications SET hidden_at=?, read_at=COALESCE(read_at, ?) "
            "WHERE hidden_at IS NULL",
            (now, now),
        )
        return cur.rowcount or 0


def gc_old(days: int = RETENTION_DAYS) -> int:
    """Drop notifications older than the retention window (called on boot),
    except the ONCE_SOURCES rows record_notification_once keeps as its memory."""
    _ensure_table()
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    keep = ",".join("?" for _ in ONCE_SOURCES)
    with _get_conn() as conn:
        cur = conn.execute(
            f"DELETE FROM notifications WHERE created_at < ? AND source NOT IN ({keep})",
            (cutoff, *ONCE_SOURCES),
        )
        return cur.rowcount or 0


# ── HTTP surface ─────────────────────────────────────────────────────────────


@router.get("")
async def api_list(limit: int = 50, unread_only: bool = False):
    return {"notifications": list_notifications(limit=limit, unread_only=unread_only)}


@router.post("")
async def api_record(request: Request):
    """Record an app-originated (frontend toast) notification.

    Body: {message: str, title?: str, source?: str, status?: str,
    session_id?: str}. Server-originated notifications never come through
    here: their writers call record_notification directly.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return Response(
            content='{"error": "message is required"}',
            status_code=400,
            media_type="application/json",
        )

    def _str(key: str) -> str:
        v = body.get(key)
        return v if isinstance(v, str) else ""

    result = record_notification(
        session_id=_str("session_id"),
        source=_str("source") or "app",
        title=_str("title"),
        message=message,
        status=_str("status"),
    )
    if result is None:
        return {"ok": False, "id": None, "deduped": False}
    return {"ok": True, **result}


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


@router.post("/hide")
async def api_hide(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    ids = body.get("ids")
    try:
        id_list = [int(i) for i in ids] if isinstance(ids, list) else None
    except (ValueError, TypeError):
        id_list = None
    if not id_list:
        return Response(
            content='{"error": "ids must be a non-empty list of integers"}',
            status_code=400,
            media_type="application/json",
        )
    return {"hidden": hide(id_list)}


@router.post("/clear")
async def api_clear():
    return {"cleared": clear_all()}
