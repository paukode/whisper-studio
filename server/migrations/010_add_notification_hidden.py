"""Soft-hide for the notification log.

The bell's per-row hide and "Clear all" set ``hidden_at`` instead of deleting,
so a mis-click is recoverable from the table and the normal 30-day GC still
bounds growth. Hidden rows are excluded from the list and the unread count.
"""

import sqlite3

VERSION = 10
DESCRIPTION = "Add hidden_at to notifications for per-row hide and clear-all"


def migrate(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE notifications ADD COLUMN hidden_at TEXT")
    except sqlite3.OperationalError:
        # Column already present (defensive _ensure_table added it first).
        pass
