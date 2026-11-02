"""Cron run-history store.

Per-job history of scheduled-task runs, kept in the shared sessions.db
(WAL-safe, concurrent reads + single writer). Each run carries a lease: it is written as
``running`` when a job fires and updated to ``ok``/``failed`` when it
finishes, so a run interrupted by a server restart can be reconciled to
``failed (interrupted)`` on the next boot instead of hanging forever.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from server.infrastructure.paths import storage_root

log = logging.getLogger("whisper-studio")

STORAGE_DIR = storage_root()
DB_PATH = os.path.join(STORAGE_DIR, "sessions.db")

# DB paths whose cron_runs table this process has already ensured.
_ensured: set[str] = set()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def _get_conn():
    os.makedirs(STORAGE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        if DB_PATH not in _ensured:
            _ensure_table(conn)
            _ensured.add(DB_PATH)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_table(conn) -> None:
    """Create cron_runs if the migration hasn't run yet (tests, first boot).

    The numbered migration in server/migrations/006_add_cron_runs.py is the
    canonical path; this keeps the same schema for a fresh/unmigrated DB.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cron_runs (
            run_id      TEXT PRIMARY KEY,
            job_id      TEXT NOT NULL,
            job_name    TEXT NOT NULL,
            session_id  TEXT,
            status      TEXT NOT NULL,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            duration_ms INTEGER,
            text        TEXT,
            next_run    TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cron_runs_job ON cron_runs(job_id, started_at DESC)"
    )


def _row_to_run(row) -> dict:
    return {
        "run_id": row["run_id"],
        "job_id": row["job_id"],
        "job_name": row["job_name"],
        "session_id": row["session_id"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "duration_ms": row["duration_ms"],
        "text": row["text"],
        "next_run": row["next_run"],
    }


def start_run(run_id: str, job_id: str, job_name: str, session_id: str) -> None:
    """Open a run lease (status='running')."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cron_runs "
            "(run_id, job_id, job_name, session_id, status, started_at) "
            "VALUES (?, ?, ?, ?, 'running', ?)",
            (run_id, job_id, job_name, session_id or "", _utc_now_iso()),
        )


def finish_run(
    run_id: str,
    *,
    status: str,
    text: str = "",
    duration_ms: int | None = None,
    next_run: str | None = None,
    max_per_job: int = 200,
) -> None:
    """Close a run lease with its outcome, then prune old runs for that job."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE cron_runs SET status=?, text=?, duration_ms=?, "
            "next_run=?, finished_at=? WHERE run_id=?",
            (status, text, duration_ms, next_run, _utc_now_iso(), run_id),
        )
        row = conn.execute("SELECT job_id FROM cron_runs WHERE run_id=?", (run_id,)).fetchone()
        if row:
            _prune_job(conn, row["job_id"], max_per_job)


def _prune_job(conn, job_id: str, max_per_job: int) -> None:
    """Keep only the newest ``max_per_job`` runs for one job."""
    conn.execute(
        """
        DELETE FROM cron_runs
        WHERE job_id = ?
          AND run_id NOT IN (
            SELECT run_id FROM cron_runs WHERE job_id = ?
            ORDER BY started_at DESC LIMIT ?
          )
        """,
        (job_id, job_id, max(1, int(max_per_job))),
    )


def list_runs(job_id: str, limit: int = 50) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM cron_runs WHERE job_id=? ORDER BY started_at DESC LIMIT ?",
            (job_id, max(1, min(int(limit), 500))),
        ).fetchall()
    return [_row_to_run(r) for r in rows]


def recent_runs(limit: int = 200) -> list[dict]:
    """Newest runs across all jobs: drives the sidebar unread badges."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT run_id, job_id, job_name, session_id, status, "
            "started_at, finished_at, duration_ms, next_run "
            "FROM cron_runs ORDER BY started_at DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
    # text omitted here on purpose: the badge feed only needs metadata.
    return [
        {
            "run_id": r["run_id"],
            "job_id": r["job_id"],
            "job_name": r["job_name"],
            "session_id": r["session_id"],
            "status": r["status"],
            "started_at": r["started_at"],
        }
        for r in rows
    ]


def last_status(job_id: str) -> str | None:
    """Status of the most recent run for a job, or None if it never ran."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM cron_runs WHERE job_id=? ORDER BY started_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    return row["status"] if row else None


def reconcile_interrupted() -> int:
    """Mark EVERY 'running' lease as failed (interrupted). Called at boot: the
    scheduler was down, so no live run can own any lease, even one opened
    seconds before the restart."""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE cron_runs SET status='failed', "
            "text=COALESCE(NULLIF(text,''), '[interrupted] server restarted mid-run'), "
            "finished_at=? WHERE status='running'",
            (_utc_now_iso(),),
        )
        return cur.rowcount or 0


def delete_job_runs(job_ids: list[str]) -> int:
    """Purge all run-history rows for the given job ids.

    Called when a job is deleted so its runs stop leaking into recent_runs (the
    sidebar feed) and per-job history. Returns the number of rows removed.
    """
    ids = [j for j in job_ids if j]
    if not ids:
        return 0
    with _get_conn() as conn:
        placeholders = ",".join("?" for _ in ids)
        cur = conn.execute(
            f"DELETE FROM cron_runs WHERE job_id IN ({placeholders})",
            tuple(ids),
        )
        return cur.rowcount or 0


def repoint_session(old_session_id: str, new_session_id: str) -> None:
    """Move a deleted session's run history to the inbox so it isn't lost."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE cron_runs SET session_id=? WHERE session_id=?",
            (new_session_id, old_session_id),
        )
