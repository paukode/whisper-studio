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
from datetime import datetime, timedelta, timezone

log = logging.getLogger("whisper-studio")

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
DB_PATH = os.path.join(STORAGE_DIR, "sessions.db")

# Runs left in 'running' longer than this on startup are treated as
# interrupted (the process died mid-execution) and marked failed.
STALE_RUN_SEC = 15 * 60


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
    except sqlite3.DatabaseError as e:
        log.warning("cron_history: failed to set sqlite pragmas: %s", e)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_table() -> None:
    """Create cron_runs if the migration hasn't run yet (tests, first boot).

    The numbered migration in server/migrations/006_add_cron_runs.py is the
    canonical path; this is a defensive net so history writes never crash on
    a fresh/unmigrated DB.
    """
    try:
        with _get_conn() as conn:
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
    except sqlite3.DatabaseError as e:
        log.warning("cron_history: ensure table failed: %s", e)


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
    _ensure_table()
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cron_runs "
                "(run_id, job_id, job_name, session_id, status, started_at) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (run_id, job_id, job_name, session_id or "", _utc_now_iso()),
            )
    except sqlite3.DatabaseError as e:
        log.warning("cron_history: start_run failed: %s", e)


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
    _ensure_table()
    try:
        with _get_conn() as conn:
            conn.execute(
                "UPDATE cron_runs SET status=?, text=?, duration_ms=?, "
                "next_run=?, finished_at=? WHERE run_id=?",
                (status, text, duration_ms, next_run, _utc_now_iso(), run_id),
            )
            row = conn.execute("SELECT job_id FROM cron_runs WHERE run_id=?", (run_id,)).fetchone()
            if row:
                _prune_job(conn, row["job_id"], max_per_job)
    except sqlite3.DatabaseError as e:
        log.warning("cron_history: finish_run failed: %s", e)


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
    _ensure_table()
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cron_runs WHERE job_id=? ORDER BY started_at DESC LIMIT ?",
                (job_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [_row_to_run(r) for r in rows]
    except sqlite3.DatabaseError as e:
        log.warning("cron_history: list_runs failed: %s", e)
        return []


def recent_runs(limit: int = 200) -> list[dict]:
    """Newest runs across all jobs — drives the sidebar unread badges."""
    _ensure_table()
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT run_id, job_id, job_name, session_id, status, "
                "started_at, finished_at, duration_ms, next_run "
                "FROM cron_runs ORDER BY started_at DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        # text omitted here on purpose — the badge feed only needs metadata.
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
    except sqlite3.DatabaseError as e:
        log.warning("cron_history: recent_runs failed: %s", e)
        return []


def last_status(job_id: str) -> str | None:
    """Status of the most recent run for a job, or None if it never ran."""
    _ensure_table()
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT status FROM cron_runs WHERE job_id=? ORDER BY started_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return row["status"] if row else None
    except sqlite3.DatabaseError:
        return None


def reconcile_stale(*, all_running: bool = False) -> int:
    """Mark 'running' leases as failed (interrupted).

    On BOOT (``all_running=True``) EVERY 'running' row is reconciled regardless
    of age: the scheduler was down, so no live run can own any lease — even one
    opened seconds before the restart is dead, and the 15-min cutoff would leave
    it hanging forever. A periodic reconciler (``all_running`` left False) keeps
    the ``STALE_RUN_SEC`` cutoff so it never kills a run that is legitimately
    still executing.
    """
    _ensure_table()
    try:
        with _get_conn() as conn:
            if all_running:
                cur = conn.execute(
                    "UPDATE cron_runs SET status='failed', "
                    "text=COALESCE(NULLIF(text,''), '[interrupted] server restarted mid-run'), "
                    "finished_at=? WHERE status='running'",
                    (_utc_now_iso(),),
                )
            else:
                cutoff = (
                    (datetime.now(timezone.utc) - timedelta(seconds=STALE_RUN_SEC))
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                cur = conn.execute(
                    "UPDATE cron_runs SET status='failed', "
                    "text=COALESCE(NULLIF(text,''), '[interrupted] server restarted mid-run'), "
                    "finished_at=? WHERE status='running' AND started_at < ?",
                    (_utc_now_iso(), cutoff),
                )
            return cur.rowcount or 0
    except sqlite3.DatabaseError as e:
        log.warning("cron_history: reconcile_stale failed: %s", e)
        return 0


def delete_job_runs(job_ids: list[str]) -> int:
    """Purge all run-history rows for the given job ids.

    Called when a job is deleted so its runs stop leaking into recent_runs (the
    sidebar feed) and per-job history. Returns the number of rows removed.
    """
    ids = [j for j in job_ids if j]
    if not ids:
        return 0
    _ensure_table()
    try:
        with _get_conn() as conn:
            placeholders = ",".join("?" for _ in ids)
            cur = conn.execute(
                f"DELETE FROM cron_runs WHERE job_id IN ({placeholders})",
                tuple(ids),
            )
            return cur.rowcount or 0
    except sqlite3.DatabaseError as e:
        log.warning("cron_history: delete_job_runs failed: %s", e)
        return 0


def repoint_session(old_session_id: str, new_session_id: str) -> None:
    """Move a deleted session's run history to the inbox so it isn't lost."""
    _ensure_table()
    try:
        with _get_conn() as conn:
            conn.execute(
                "UPDATE cron_runs SET session_id=? WHERE session_id=?",
                (new_session_id, old_session_id),
            )
    except sqlite3.DatabaseError as e:
        log.warning("cron_history: repoint_session failed: %s", e)
