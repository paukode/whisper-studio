"""Durable session-scoped attachments.

Uploads used to live only in a process-global dict with a 3-hour TTL, so a
file's content silently left the conversation on the next turn and died with
every restart. The attachments table makes the extracted content durable;
binding, retention, and GC live in server/attachment_store.py.
"""

import sqlite3

VERSION = 11
DESCRIPTION = "Add attachments table for durable session-scoped attachments"


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("""
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
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id)")
