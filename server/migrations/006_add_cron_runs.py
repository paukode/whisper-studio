"""Per-job cron run history.

Replaces the flat, global, capped-100, never-surfaced data/cron_results.json
with a WAL-safe table in sessions.db. Carries the run lease (status='running')
so a fire interrupted by a restart can be reconciled to 'failed' instead of
hanging. Indexed by (job_id, started_at DESC) for the per-job history drawer.
"""

import sqlite3

VERSION = 6
DESCRIPTION = "Add cron_runs table for per-job scheduled-task run history"


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cron_runs (
            run_id      TEXT PRIMARY KEY,
            job_id      TEXT NOT NULL,
            job_name    TEXT NOT NULL,
            session_id  TEXT,
            status      TEXT NOT NULL,          -- 'running' | 'ok' | 'failed'
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            duration_ms INTEGER,
            text        TEXT,
            next_run    TEXT
        )
        """
    )
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cron_runs_job ON cron_runs(job_id, started_at DESC)"
        )
    except sqlite3.OperationalError:
        pass
