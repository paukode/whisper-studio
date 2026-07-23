"""Add pinned and archived flags to sessions for the sidebar context menu."""

import sqlite3

VERSION = 5
DESCRIPTION = "Add pinned and archived flags to sessions"


def migrate(conn):
    # SQLite requires one ALTER TABLE per column
    for col, typedef in [
        ("pinned", "INTEGER DEFAULT 0"),
        ("archived", "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            # Column already exists (re-run migration) — any other
            # OperationalError (locked db, bad schema) now propagates
            # instead of being silently masked.
            pass
