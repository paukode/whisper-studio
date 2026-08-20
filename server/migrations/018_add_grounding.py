"""Per-answer grounding sources ("Answer sources").

The grounding SSE event only carried counts (folders searched / passages
injected), so once a turn ended the passages behind the answer were gone. This
table persists each grounded turn's retrieved passages with their provenance
(matched words / similar meaning / entity links), keyed by the grounding id the
event now carries; the chat chip resolves it via
GET /api/sessions/{id}/grounding/{grounding_id}. Rows die with their session.
"""

import sqlite3

VERSION = 18
DESCRIPTION = "Add grounding table for per-answer grounding sources"


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS grounding (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sources TEXT NOT NULL DEFAULT '[]'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_grounding_session ON grounding(session_id)")
