"""Persistent notification log.

notify_user was a fire-and-forget 5-second toast: a user away from the screen
lost the message forever (only recoverable by expanding the raw tool trace).
This table gives every notification — from chat turns, agents, and cron runs —
a durable, queryable record behind the header bell.
"""

import sqlite3

VERSION = 8
DESCRIPTION = "Add notifications table for the persistent notify_user log"


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT,
            source      TEXT NOT NULL CHECK (source IN ('chat', 'agent', 'cron')),
            title       TEXT,
            message     TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'normal',
            created_at  TEXT NOT NULL,
            read_at     TEXT
        )
        """
    )
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(read_at, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_notifications_session ON notifications(session_id, created_at DESC)",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
