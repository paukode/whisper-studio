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
import shutil
import sqlite3
import time
from contextlib import contextmanager

from server.infrastructure.paths import data_root, storage_root

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
        source_path TEXT NOT NULL DEFAULT '',
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
            " data, frames, source_path, created, last_used) "
            "VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                record.get("source_path", ""),
                now,
                now,
            ),
        )


# ---------------------------------------------------------------------------
# Original bytes on disk
# ---------------------------------------------------------------------------
#
# Extraction is lossy on purpose: a 10k-row spreadsheet becomes a header plus a
# 20-row sample, because the full markdown would not fit any context window.
# That sample is fine to read and useless to compute over — asked for a total,
# a model summing 20 of 10,000 rows produces a confident wrong number. So the
# upload's bytes are kept next to the extracted text and the path travels into
# the prompt, which lets run_python open the real file with pandas.


def source_files_dir() -> str:
    """Directory holding uploads' original bytes. Under data_root so it moves
    with a packaged install's app home."""
    return os.path.join(data_root(), "attachments")


def save_source_file(aid: str, filename: str, content: bytes) -> str:
    """Persist an upload's bytes; returns the path, or '' if it could not be
    written. Never raises: losing the original degrades analysis quality, it
    does not break the attachment."""
    ext = os.path.splitext(filename)[1].lower()[:16]
    directory = source_files_dir()
    path = os.path.join(directory, f"{aid}{ext}")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        return path
    except OSError as e:
        log.warning("could not keep source bytes for %s: %s", filename, e)
        return ""


def _row_value(row, column: str, default=""):
    """Read a column that may not exist yet on a database the migration
    runner has not reached (tests reach this module directly)."""
    try:
        return row[column]
    except (IndexError, KeyError):
        return default


def sweep_source_files() -> int:
    """Delete files whose attachment row is gone, so retention and session
    deletion need no path bookkeeping of their own. Returns files removed.

    Files younger than the unbound TTL are skipped: an upload writes its bytes
    before its row, and a sweep in that window would delete a live attachment.
    """
    directory = source_files_dir()
    if not os.path.isdir(directory):
        return 0
    with _get_conn() as conn:
        live = {r[0] for r in conn.execute("SELECT id FROM attachments")}
    cutoff = time.time() - UNBOUND_TTL_SECONDS
    removed = 0
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if os.path.splitext(name)[0] in live:
            continue
        try:
            if os.path.getmtime(path) > cutoff:
                continue
            shutil.rmtree(path) if os.path.isdir(path) else os.unlink(path)
            removed += 1
        except OSError:
            continue
    return removed


def _row_to_record(row, include_binary: bool = True) -> dict:
    rec = {
        "kind": row["kind"],
        "filename": row["filename"],
        "media_type": row["media_type"],
        "outline": row["outline"],
        "sections": json.loads(row["sections"] or "[]"),
        "created": row["created"],
    }
    # The original bytes, when we kept them. This is what lets run_python
    # compute over the WHOLE file instead of the sample in the prompt.
    source = _row_value(row, "source_path")
    if source and os.path.exists(source):
        rec["source_path"] = source
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
        deleted = cur.rowcount
    sweep_source_files()
    return deleted


_ensure_table()
