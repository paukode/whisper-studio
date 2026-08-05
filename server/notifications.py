"""Persistent notification log.

Two write paths land in one log: the ``notify_user`` branch in
server/tool_router.py calls :func:`record_notification` best-effort (chat
turns, agent runs, and cron jobs all dispatch through ``route_tool``), and
frontend toasts POST themselves here (``POST /api/notifications``, wired
centrally in the uiStore's addToast) so the bell keeps a reviewable history
after the 5-second toast is gone. Each row carries an ``source`` slug
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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.infrastructure.paths import storage_root

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

STORAGE_DIR = storage_root()
DB_PATH = os.path.join(STORAGE_DIR, "sessions.db")

RETENTION_DAYS = 30
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
    """Defensive net alongside migrations 008/010/012 (tests, fresh boots)."""
    try:
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
            for alter in (
                "ALTER TABLE notifications ADD COLUMN hidden_at TEXT",
                "ALTER TABLE notifications ADD COLUMN count INTEGER NOT NULL DEFAULT 1",
            ):
                try:
                    conn.execute(alter)
                except sqlite3.OperationalError:
                    pass
    except sqlite3.DatabaseError as e:
        log.warning("notifications: ensure table failed: %s", e)


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
        (datetime.now(timezone.utc) - timedelta(seconds=DEDUPE_WINDOW_SECONDS))
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
    _ensure_table()
    fields = {
        "session_id": session_id or "",
        "title": (title or "")[:200],
        "message": message[:4000],
        "status": status if status in _VALID_STATUSES else "normal",
    }
    try:
        with _get_conn() as conn:
            try:
                return _insert_or_bump(conn, source=source, **fields)
            except sqlite3.IntegrityError:
                # Pre-012 table (CHECK source IN ('chat','agent','cron')) that
                # _ensure_table's CREATE IF NOT EXISTS could not relax: keep
                # the row rather than the tag.
                return _insert_or_bump(conn, source="chat", **fields)
    except sqlite3.DatabaseError as e:
        log.warning("notifications: record failed: %s", e)
        return None


def list_notifications(*, limit: int = 50, unread_only: bool = False) -> list[dict]:
    """Visible (non-hidden) notifications, newest first."""
    _ensure_table()
    where = "WHERE hidden_at IS NULL"
    if unread_only:
        where += " AND read_at IS NULL"
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
                "SELECT COUNT(*) AS n FROM notifications "
                "WHERE read_at IS NULL AND hidden_at IS NULL"
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


def hide(ids: list[int]) -> int:
    """Soft-hide specific notifications (a hidden row also counts as read)."""
    if not ids:
        return 0
    _ensure_table()
    now = _utc_now_iso()
    try:
        with _get_conn() as conn:
            placeholders = ",".join("?" for _ in ids)
            cur = conn.execute(
                f"UPDATE notifications SET hidden_at=?, read_at=COALESCE(read_at, ?) "
                f"WHERE id IN ({placeholders}) AND hidden_at IS NULL",
                (now, now, *ids),
            )
            return cur.rowcount or 0
    except sqlite3.DatabaseError as e:
        log.warning("notifications: hide failed: %s", e)
        return 0


def clear_all() -> int:
    """Soft-hide every visible notification (the bell's "Clear all")."""
    _ensure_table()
    now = _utc_now_iso()
    try:
        with _get_conn() as conn:
            cur = conn.execute(
                "UPDATE notifications SET hidden_at=?, read_at=COALESCE(read_at, ?) "
                "WHERE hidden_at IS NULL",
                (now, now),
            )
            return cur.rowcount or 0
    except sqlite3.DatabaseError as e:
        log.warning("notifications: clear failed: %s", e)
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


@router.post("")
async def api_record(request: Request):
    """Record an app-originated (frontend toast) notification.

    Body: {message: str, title?: str, source?: str, status?: str,
    session_id?: str}. Server-originated notifications (notify_user) never
    come through here — they are recorded at the route_tool choke point.
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
