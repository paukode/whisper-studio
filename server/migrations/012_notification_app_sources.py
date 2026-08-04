"""Open the notification log to app-originated toasts.

Frontend toasts (errors, completions, warnings) now land in the persistent
log too, tagged with a free-form source slug ("app", "model-download",
"recording", ...). The original table CHECK-constrained source to
('chat', 'agent', 'cron'), so the table is rebuilt without the CHECK.
Adds ``count`` for the tight-loop dedupe: identical toasts fired within a
few seconds collapse into one row with a repeat count instead of spamming
the bell.
"""

import sqlite3

VERSION = 12
DESCRIPTION = "Rebuild notifications without source CHECK; add dedupe count"


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications_new (
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
    # The defensive _ensure_table in server/notifications.py may already have
    # created the table with or without hidden_at/count; copy whatever columns
    # both sides share.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(notifications)").fetchall()]
    if cols:
        shared = [
            c
            for c in (
                "id",
                "session_id",
                "source",
                "title",
                "message",
                "status",
                "created_at",
                "read_at",
                "hidden_at",
                "count",
            )
            if c in cols
        ]
        col_list = ", ".join(shared)
        conn.execute(
            f"INSERT INTO notifications_new ({col_list}) SELECT {col_list} FROM notifications"
        )
        conn.execute("DROP TABLE notifications")
    conn.execute("ALTER TABLE notifications_new RENAME TO notifications")
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(read_at, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_notifications_session ON notifications(session_id, created_at DESC)",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
