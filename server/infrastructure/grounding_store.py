"""Durable per-answer grounding sources ("Answer sources").

Each index-grounded chat turn retrieves passages (server/index/pipeline.py's
``retrieve_grounding``) and injects them into the prompt. The wire event only
says HOW MUCH was injected (searched/passages counts); this table keeps WHAT —
the passage list with retrieval provenance — keyed by a server-minted grounding
id that rides the ``grounding`` SSE event and is stored on the assistant
message. The chat's grounding chip resolves it back through
``GET /api/sessions/{session_id}/grounding/{grounding_id}``.

Lifetime is tied to the session: rows die with it (``_delete_session_sync``),
and a per-session cap trims the oldest rows so an eternal session can't grow
the table without bound (a trimmed row degrades to the counts-only chip).
"""

import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from server.infrastructure.paths import storage_root

log = logging.getLogger("whisper-studio")

STORAGE_DIR = storage_root()
DB_PATH = os.path.join(STORAGE_DIR, "sessions.db")

# Oldest rows beyond this many per session are trimmed on save. Generous: one
# row per grounded turn, each a few KB of JSON.
MAX_PER_SESSION = 500

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS grounding (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        sources TEXT NOT NULL DEFAULT '[]'
    )
"""
_INDEX_DDL = "CREATE INDEX IF NOT EXISTS idx_grounding_session ON grounding(session_id)"


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
    runner, so create the current schema directly; migration 018 stays the
    upgrade path for databases the app owns."""
    with _get_conn() as conn:
        conn.execute(_TABLE_DDL)
        conn.execute(_INDEX_DDL)


def save_grounding(session_id: str, sources: list[dict]) -> str:
    """Persist one turn's grounding sources; returns the minted grounding id."""
    gid = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO grounding (id, session_id, created_at, sources) VALUES (?, ?, ?, ?)",
            (gid, session_id, now, json.dumps(sources)),
        )
        conn.execute(
            "DELETE FROM grounding WHERE session_id = ? AND id NOT IN ("
            "  SELECT id FROM grounding WHERE session_id = ?"
            "  ORDER BY created_at DESC, id LIMIT ?)",
            (session_id, session_id, MAX_PER_SESSION),
        )
    return gid


def get_grounding(session_id: str, grounding_id: str) -> list[dict] | None:
    """The persisted source list, or None when unknown (wrong session included —
    the session id scopes the lookup so one session can't read another's)."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT sources FROM grounding WHERE id = ? AND session_id = ?",
            (grounding_id, session_id),
        ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["sources"])
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, list) else None


_ensure_table()
