"""SQLite store for the unified background-task registry.

Modeled on server/cron_history.py: shared sessions.db, WAL, defensive
``_ensure_table`` net alongside the canonical numbered migration
(server/migrations/007_add_agent_tasks.py), and a running-lease pattern
reconciled on boot.

The reconcile logic is deliberately kind-aware:

- ``agent``/``workflow`` rows are in-process asyncio work; a restart always
  kills them, so any 'running' row from another server pid is interrupted.
- ``shell`` rows own a real child process in its own process group, which
  survives the server restart. Those are probed: dead pid -> interrupted with
  the output-file tail; live pid -> RE-ADOPTED (status stays 'running') with a
  liveness watcher that closes the row when the pid vanishes. The exit code is
  unknowable after re-parenting, recorded as NULL with an explanatory note.
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

log = logging.getLogger("whisper-studio")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
DB_PATH = os.path.join(STORAGE_DIR, "sessions.db")

FINISHED_TTL_S = 24 * 3600
MAX_FINISHED = 200
_ADOPT_POLL_S = 2.0

_COLUMNS = (
    "task_id, kind, session_id, title, command, status, exit_code, pid, "
    "output_path, result_text, meta, owner_pid, created_at, updated_at, finished_at"
)


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
        log.warning("tasks.registry: failed to set sqlite pragmas: %s", e)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_table() -> None:
    """Create agent_tasks if migration 007 hasn't run yet (tests, first boot)."""
    try:
        with _get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id     TEXT PRIMARY KEY,
                    kind        TEXT NOT NULL,
                    session_id  TEXT,
                    title       TEXT NOT NULL,
                    command     TEXT,
                    status      TEXT NOT NULL,
                    exit_code   INTEGER,
                    pid         INTEGER,
                    output_path TEXT,
                    result_text TEXT,
                    meta        TEXT,
                    owner_pid   INTEGER NOT NULL,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    finished_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_tasks_session "
                "ON agent_tasks(session_id, created_at DESC)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status)")
    except sqlite3.DatabaseError as e:
        log.warning("tasks.registry: ensure table failed: %s", e)


def _row_to_task(row) -> dict:
    task = {k: row[k] for k in row.keys()}
    raw_meta = task.get("meta")
    if raw_meta:
        try:
            task["meta"] = json.loads(raw_meta)
        except (TypeError, ValueError):
            task["meta"] = {}
    else:
        task["meta"] = {}
    return task


def create_task(
    kind: str,
    *,
    session_id: str = "",
    title: str,
    command: str | None = None,
    output_path: str | None = None,
    meta: dict | None = None,
    task_id: str | None = None,
) -> str:
    """Register a new running task and return its id."""
    if kind not in ("shell", "agent", "workflow", "ci"):
        raise ValueError(f"unknown task kind: {kind!r}")
    tid = task_id or uuid.uuid4().hex[:12]
    now = _utc_now_iso()
    _ensure_table()
    try:
        with _get_conn() as conn:
            conn.execute(
                f"INSERT INTO agent_tasks ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, 'running', NULL, NULL, ?, NULL, ?, ?, ?, ?, NULL)",
                (
                    tid,
                    kind,
                    session_id or "",
                    (title or "").strip()[:200] or kind,
                    command,
                    output_path,
                    json.dumps(meta) if meta else None,
                    os.getpid(),
                    now,
                    now,
                ),
            )
    except sqlite3.DatabaseError as e:
        log.warning("tasks.registry: create_task failed: %s", e)
    return tid


def attach_pid(task_id: str, pid: int) -> None:
    """Record the process-group leader pid of a shell task."""
    _ensure_table()
    try:
        with _get_conn() as conn:
            conn.execute(
                "UPDATE agent_tasks SET pid=?, updated_at=? WHERE task_id=?",
                (pid, _utc_now_iso(), task_id),
            )
    except sqlite3.DatabaseError as e:
        log.warning("tasks.registry: attach_pid failed: %s", e)


def finish_task(
    task_id: str,
    *,
    status: str,
    exit_code: int | None = None,
    result_text: str = "",
) -> dict | None:
    """Close a task lease; returns the finished row (for event emission).

    Finishing is idempotent and only transitions rows still marked 'running',
    so a stop racing a natural exit records exactly one terminal status.

    Retries transient sqlite lock errors: the waiter threads that call this
    are one-shot, so a busy-timeout here would otherwise strand the row as
    'running' forever.
    """
    if status not in ("completed", "failed", "stopped", "interrupted"):
        raise ValueError(f"invalid terminal status: {status!r}")
    now = _utc_now_iso()
    _ensure_table()
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            with _get_conn() as conn:
                cur = conn.execute(
                    "UPDATE agent_tasks SET status=?, exit_code=?, result_text=?, "
                    "updated_at=?, finished_at=? WHERE task_id=? AND status='running'",
                    (status, exit_code, (result_text or "")[:4000], now, now, task_id),
                )
                if not cur.rowcount:
                    return None
                _prune_finished_locked(conn)
                row = conn.execute(
                    "SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            return _row_to_task(row) if row else None
        except sqlite3.DatabaseError as e:
            last_err = e
            time.sleep(0.25 * (attempt + 1))
    log.warning("tasks.registry: finish_task failed after retries: %s", last_err)
    return None


def get_task(task_id: str) -> dict | None:
    _ensure_table()
    try:
        with _get_conn() as conn:
            row = conn.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None
    except sqlite3.DatabaseError as e:
        log.warning("tasks.registry: get_task failed: %s", e)
        return None


def list_tasks(
    *,
    session_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 100,
) -> list[dict]:
    _ensure_table()
    clauses, params = [], []
    if session_id is not None:
        clauses.append("session_id=?")
        params.append(session_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM agent_tasks {where} ORDER BY created_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
        return [_row_to_task(r) for r in rows]
    except sqlite3.DatabaseError as e:
        log.warning("tasks.registry: list_tasks failed: %s", e)
        return []


def _prune_finished_locked(conn) -> None:
    """Drop finished rows past the TTL and cap the finished backlog.

    Output files on disk are left alone (same policy as the old in-memory
    registry); GC of data/background_output is a separate concern.
    """
    cutoff = (
        datetime.fromtimestamp(time.time() - FINISHED_TTL_S, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    conn.execute(
        "DELETE FROM agent_tasks WHERE status != 'running' AND finished_at < ?",
        (cutoff,),
    )
    conn.execute(
        """
        DELETE FROM agent_tasks
        WHERE status != 'running'
          AND task_id NOT IN (
            SELECT task_id FROM agent_tasks WHERE status != 'running'
            ORDER BY finished_at DESC LIMIT ?
          )
        """,
        (MAX_FINISHED,),
    )


def _tail_of_file(path: str | None, max_bytes: int = 2048) -> str:
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def tail_lines_of_file(path: str | None, max_lines: int, max_bytes: int = 2_000_000) -> str:
    """Last ``max_lines`` lines of a file, reading at most ``max_bytes`` from
    the end — a multi-GB output file must never be slurped into memory."""
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines(keepends=True)
    return "".join(lines[-max(1, max_lines) :])


def _pid_alive(pid: int) -> bool:
    """True only when the pid exists AND we may signal it. PermissionError
    means a different uid owns it — that can never be one of our own spawned
    children, so for adoption purposes a recycled pid held by a system daemon
    must read as dead."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return False
    return True


def _system_boot_time() -> float | None:
    """Epoch seconds of the last system boot (macOS sysctl / Linux /proc)."""
    try:
        if os.path.exists("/proc/stat"):
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("btime "):
                        return float(line.split()[1])
        import re
        import subprocess

        out = subprocess.run(
            ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5
        ).stdout
        m = re.search(r"sec\s*=\s*(\d+)", out)
        if m:
            return float(m.group(1))
    except Exception as e:
        log.debug("tasks.registry: boot time probe failed: %s", e)
    return None


def _adoptable(task: dict, pid: int) -> bool:
    """Identity gate before re-adopting a pid as a surviving shell child.

    A bare liveness probe is not enough: after a machine reboot (or plain pid
    reuse) the recorded pid can belong to an unrelated process, which would
    pin the task 'running' against a stranger. Adoption requires:
    (1) the pid is alive AND signalable by us (same uid — see _pid_alive);
    (2) the system has not rebooted since the task row was created.
    """
    if not _pid_alive(pid):
        return False
    boot = _system_boot_time()
    if boot is not None:
        try:
            created = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
            if boot > created.timestamp():
                return False  # machine rebooted: the original child cannot exist
        except (KeyError, TypeError, ValueError):
            return False
    return True


def _watch_adopted(task_id: str, pid: int) -> None:
    """Liveness-poll a re-adopted shell task until its pid vanishes.

    The child was re-parented when the previous server died, so ``wait()`` is
    impossible; the exit code is honestly unknowable and recorded as NULL.
    """
    while _pid_alive(pid):
        time.sleep(_ADOPT_POLL_S)
    task = get_task(task_id)
    tail = _tail_of_file(task.get("output_path")) if task else ""
    note = "[exit code unknown: adopted after server restart]"
    finished = finish_task(
        task_id,
        status="completed",
        exit_code=None,
        result_text=f"{tail}\n{note}".strip(),
    )
    if finished:
        try:
            from server.tasks.events import emit_task_event

            emit_task_event(finished["session_id"], "task_completed", finished)
        except Exception as e:  # never let event emission kill the watcher
            log.warning("tasks.registry: adopted-task event failed: %s", e)


def reconcile_on_boot() -> dict:
    """Reconcile 'running' leases owned by a previous server process.

    Returns counts {interrupted, adopted} for the boot log.
    """
    _ensure_table()
    interrupted = adopted = 0
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_tasks WHERE status='running' AND owner_pid != ?",
                (os.getpid(),),
            ).fetchall()
        for row in rows:
            task = _row_to_task(row)
            # Another LIVE server process owns this lease (multi-process /
            # dev-parallel case): it will close its own rows — leave it alone.
            owner_pid = task.get("owner_pid")
            if owner_pid and _pid_alive(int(owner_pid)):
                continue
            pid = task.get("pid")
            if task["kind"] == "shell" and pid and _adoptable(task, int(pid)):
                adopted += 1
                threading.Thread(
                    target=_watch_adopted,
                    args=(task["task_id"], int(pid)),
                    daemon=True,
                    name=f"task-adopt-{task['task_id']}",
                ).start()
                continue
            tail = _tail_of_file(task.get("output_path"))
            finish_task(
                task["task_id"],
                status="interrupted",
                exit_code=None,
                result_text=(f"{tail}\n" if tail else "")
                + "[interrupted] server restarted mid-run",
            )
            interrupted += 1
    except sqlite3.DatabaseError as e:
        log.warning("tasks.registry: reconcile_on_boot failed: %s", e)
    if interrupted or adopted:
        log.info("tasks.registry: boot reconcile interrupted=%d adopted=%d", interrupted, adopted)
    return {"interrupted": interrupted, "adopted": adopted}
