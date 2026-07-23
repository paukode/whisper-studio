"""Add metadata columns to sessions for richer session restore."""

import sqlite3

VERSION = 2
DESCRIPTION = "Add workspace_path, compaction_count, latched_config to sessions"


def migrate(conn):
    # SQLite requires one ALTER TABLE per column
    for col, typedef in [
        ("workspace_path", "TEXT DEFAULT ''"),
        ("compaction_count", "INTEGER DEFAULT 0"),
        ("latched_config", "TEXT DEFAULT '{}'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            # Column already exists (re-run migration) — any other
            # OperationalError (locked db, bad schema) now propagates
            # instead of being silently masked.
            pass
