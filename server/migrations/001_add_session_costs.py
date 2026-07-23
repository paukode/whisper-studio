"""Add session_costs table for per-turn cost tracking."""

VERSION = 1
DESCRIPTION = "Add session_costs table for per-turn cost tracking"


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0.0,
            api_duration_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_session_costs_session
        ON session_costs(session_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_session_costs_created
        ON session_costs(created_at)
    """)
