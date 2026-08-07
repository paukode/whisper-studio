"""Durable, session-scoped attachment storage.

Uploaded files historically lived only in an in-memory dict with a 3-hour TTL,
so their content vanished from a conversation on the very next turn (the chat
history persists only the typed question) and on every backend restart. This
module makes the extracted content durable in sessions.db so attachments are
session state, not turn state:

  - ``server/attachments.py`` writes every upload through here (its in-memory
    dict stays as a hot cache in front of this table).
  - The first chat turn that references an upload binds it to that session
    (``bind_to_session``), which also refreshes ``last_used``.
  - Bound attachments live ``RETENTION_DAYS`` from their last use; every turn
    that references them slides the window. Unbound uploads (never used in a
    chat) keep the short in-memory TTL since nothing owns them.
  - Deleting a session deletes its attachments (see ``_delete_session_sync``).

Row shape mirrors the in-memory records: documents keep extracted ``text`` +
``outline``/``sections``; images keep base64 ``data`` + OCR text in ``text``;
video documents additionally keep sampled ``frames``.
"""

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager

from server.infrastructure.paths import storage_root

log = logging.getLogger("whisper-studio")

STORAGE_DIR = storage_root()
DB_PATH = os.path.join(STORAGE_DIR, "sessions.db")

# Bound attachments survive this long past their last referencing turn.
RETENTION_DAYS = 30
RETENTION_SECONDS = RETENTION_DAYS * 24 * 3600
# Unbound uploads (never referenced by a chat turn) are orphans; give them the
# same grace the in-memory cache does before sweeping.
UNBOUND_TTL_SECONDS = 3 * 3600

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS attachments (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL,
        filename TEXT NOT NULL,
        media_type TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL DEFAULT '',
        outline TEXT NOT NULL DEFAULT '',
        sections TEXT NOT NULL DEFAULT '[]',
        data TEXT NOT NULL DEFAULT '',
        frames TEXT NOT NULL DEFAULT '[]',
        created REAL NOT NULL,
        last_used REAL NOT NULL
    )
"""
_INDEX_DDL = "CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id)"


@contextmanager
def _get_conn():
    os.makedirs(STORAGE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.DatabaseError as e:
        log.warning("failed to set sqlite pragmas: %s", e)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_table():
    """Tests (and any importer) reach this module without the app's migration
    runner, so create the current schema directly; migration 011 stays the
    upgrade path for databases the app owns."""
    with _get_conn() as conn:
        conn.execute(_TABLE_DDL)
        conn.execute(_INDEX_DDL)


def save_attachment(aid: str, record: dict) -> None:
    """Write-through an upload's in-memory record. Unbound until a chat turn
    references it."""
    now = record.get("created", time.time())
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO attachments "
            "(id, session_id, kind, filename, media_type, text, outline, sections, "
            " data, frames, created, last_used) "
            "VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                aid,
                record.get("kind", "document"),
                record.get("filename", "unknown"),
                record.get("media_type", ""),
                # Images keep their OCR text in the shared text column.
                record.get("text") or record.get("ocr_text") or "",
                record.get("outline", ""),
                json.dumps(record.get("sections") or []),
                record.get("data", ""),
                json.dumps(record.get("frames") or []),
                now,
                now,
            ),
        )


def _row_to_record(row, include_binary: bool = True) -> dict:
    rec = {
        "kind": row["kind"],
        "filename": row["filename"],
        "media_type": row["media_type"],
        "outline": row["outline"],
        "sections": json.loads(row["sections"] or "[]"),
        "created": row["created"],
    }
    if row["kind"] == "image":
        rec["ocr_text"] = row["text"]
        if include_binary:
            rec["data"] = row["data"]
    else:
        rec["text"] = row["text"]
        if include_binary:
            frames = json.loads(row["frames"] or "[]")
            if frames:
                rec["frames"] = frames
    return rec


def get_attachment(aid: str) -> dict | None:
    """Fetch one attachment in the in-memory record shape, or None."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (aid,)).fetchone()
    return _row_to_record(row) if row else None


def load_session_attachments(session_id: str, include_binary: bool = False) -> dict[str, dict]:
    """Every attachment bound to a session, keyed by id.

    ``include_binary=False`` (the default) skips image base64 and video frames:
    tool access (analyze_document) only needs text/outline/sections, and bound
    images can be multiple MB each.
    """
    if not session_id:
        return {}
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM attachments WHERE session_id = ?", (session_id,)
        ).fetchall()
    return {row["id"]: _row_to_record(row, include_binary=include_binary) for row in rows}


def bind_to_session(aids: list[str], session_id: str) -> None:
    """Bind uploads to the session that referenced them and slide the
    retention window. An id already bound elsewhere is left with its original
    owner (uploads are minted per message, so this is a defensive no-op)."""
    if not aids or not session_id:
        return
    now = time.time()
    with _get_conn() as conn:
        for aid in aids:
            conn.execute(
                "UPDATE attachments SET session_id = ? WHERE id = ? AND session_id = ''",
                (session_id, aid),
            )
            conn.execute(
                "UPDATE attachments SET last_used = ? WHERE id = ? AND session_id = ?",
                (now, aid, session_id),
            )


def gc(now: float | None = None) -> int:
    """Sweep expired rows: unbound uploads past the short TTL, bound
    attachments idle past the retention window. Returns rows deleted."""
    now = now if now is not None else time.time()
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM attachments WHERE "
            "(session_id = '' AND created < ?) OR "
            "(session_id != '' AND last_used < ?)",
            (now - UNBOUND_TTL_SECONDS, now - RETENTION_SECONDS),
        )
        return cur.rowcount


_ensure_table()
