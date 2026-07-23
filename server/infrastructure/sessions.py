import asyncio
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import APIRouter

log = logging.getLogger("whisper-studio")

router = APIRouter(tags=["sessions"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
DB_PATH = os.path.join(STORAGE_DIR, "sessions.db")

# Roles that are part of the *prompt* sent to Claude. Anything else
# (cron_event, future system_internal, etc.) is UI-only and must be
# filtered out before building the Bedrock messages array.
PROMPT_ROLES = frozenset({"user", "assistant"})

# Hard cap on inline cron_event rows kept in chat_history per session.
# Older cron_events are dropped on append; the durable per-run record
# lives in the cron_runs table (see server/cron_history.py), so the
# Settings panel's history drawer never loses data when the cap trims.
MAX_CRON_EVENTS_PER_SESSION = 50

# Well-known pinned session that catches cron runs whose owning session was
# deleted or archived, so a firing is never emitted into the void. Rendered
# in the sidebar like any pinned session; guarded from deletion.
CRON_INBOX_ID = "__cron_inbox__"
CRON_INBOX_TITLE = "Scheduled Reports"

# Per-session asyncio locks shared by append_message and the session
# save endpoints. Both writers must hold the lock to prevent the
# frontend's UPSERT from clobbering background cron appends.
_session_locks: dict[str, asyncio.Lock] = {}


def _lock_for(session_id: str) -> asyncio.Lock:
    # setdefault is a single atomic dict op, so two coroutines racing to create
    # the lock for a new session always get the SAME object (the throwaway
    # Lock() when the key already exists is negligible). A get/if-None/assign
    # would be a check-then-act that could hand out two different locks.
    return _session_locks.setdefault(session_id, asyncio.Lock())


# Columns bolted onto the base sessions schema by later migrations. Tests
# (and any other importer) reach this module without the app's migration
# runner, so _ensure_db must produce the full current schema itself; the
# migrations stay the upgrade path for databases the app owns and tolerate
# these columns already existing.
_MIGRATED_SESSION_COLUMNS = (
    ("workspace_path", "TEXT DEFAULT ''"),  # migration 002
    ("compaction_count", "INTEGER DEFAULT 0"),  # migration 002
    ("latched_config", "TEXT DEFAULT '{}'"),  # migration 002
    ("pinned", "INTEGER DEFAULT 0"),  # migration 005
    ("archived", "INTEGER DEFAULT 0"),  # migration 005
    ("goal", "TEXT DEFAULT ''"),  # migration 009 (WS-E goal loop)
    ("goal_state", "TEXT DEFAULT '{}'"),  # migration 009 (WS-E goal loop)
)


def _ensure_db():
    os.makedirs(STORAGE_DIR, exist_ok=True)
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'Untitled Session',
                custom_title INTEGER NOT NULL DEFAULT 0,
                generated_title INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                segments TEXT NOT NULL DEFAULT '[]',
                chat_history TEXT NOT NULL DEFAULT '[]',
                speaker_names TEXT NOT NULL DEFAULT '{}'
            )
        """)
        # Replay the additive column migrations so a table created by an
        # older checkout (or by the CREATE above on a fresh database) ends
        # up with the current schema either way.
        present = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        for col, typedef in _MIGRATED_SESSION_COLUMNS:
            if col not in present:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typedef}")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_updated
            ON sessions(updated_at DESC)
        """)


@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # WAL allows readers and a single writer to proceed concurrently;
    # busy_timeout makes us wait (instead of raising) when the writer is held.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.DatabaseError as e:
        log.warning("failed to set sqlite pragmas: %s", e)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _safe_col(row, col, default=""):
    """Safely read a column that may not exist yet (pre-migration)."""
    try:
        return row[col]
    except (IndexError, KeyError):
        return default


def _row_to_dict(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "customTitle": bool(row["custom_title"]),
        "generatedTitle": bool(row["generated_title"]),
        "createdAt": row["created_at"],
        "date": row["updated_at"],
        "segments": json.loads(row["segments"]),
        "chatHistory": json.loads(row["chat_history"]),
        "speakerNames": json.loads(row["speaker_names"]),
        "workspacePath": _safe_col(row, "workspace_path", ""),
        "compactionCount": int(_safe_col(row, "compaction_count", 0)),
        "latchedConfig": json.loads(_safe_col(row, "latched_config", "{}")),
        "goal": _safe_col(row, "goal", ""),
        "goalState": json.loads(_safe_col(row, "goal_state", "{}") or "{}"),
    }


def _row_to_summary(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "customTitle": bool(row["custom_title"]),
        "generatedTitle": bool(row["generated_title"]),
        "createdAt": row["created_at"],
        "date": row["updated_at"],
        "segmentCount": len(json.loads(row["segments"])),
        "chatCount": len(json.loads(row["chat_history"])),
        "workspacePath": _safe_col(row, "workspace_path", ""),
        "pinned": bool(_safe_col(row, "pinned", 0)),
        "archived": bool(_safe_col(row, "archived", 0)),
    }


def visible_chat_history(history: list[dict]) -> list[dict]:
    """Drop non-prompt roles before building a Bedrock request.

    cron_event (and any future UI-only role) lives in chat_history so the
    chat renders it on resume, but it must not enter Claude's context.
    """
    if not history:
        return history
    return [m for m in history if m.get("role") in PROMPT_ROLES]


def _enforce_cron_event_cap(history: list[dict]) -> list[dict]:
    """Keep at most MAX_CRON_EVENTS_PER_SESSION cron_event rows.

    Drops the oldest cron_events (by position) when the cap is exceeded.
    Non-cron-event entries are untouched.
    """
    cron_positions = [i for i, m in enumerate(history) if m.get("role") == "cron_event"]
    if len(cron_positions) <= MAX_CRON_EVENTS_PER_SESSION:
        return history
    drop = set(cron_positions[: len(cron_positions) - MAX_CRON_EVENTS_PER_SESSION])
    return [m for i, m in enumerate(history) if i not in drop]


def _ui_only_rows(history: list[dict]) -> list[dict]:
    """Return the subset of history that the backend owns (not the frontend).

    Used by save_session/beacon to merge cron_events written between the
    last frontend hydrate and the current PUT — otherwise the frontend's
    UPSERT would clobber them.
    """
    return [m for m in history if m.get("role") not in PROMPT_ROLES]


async def append_message(session_id: str, message: dict) -> bool:
    """Atomically append a message to a session's chat_history.

    Holds the per-session asyncio.Lock so a concurrent frontend PUT
    cannot interleave a read-modify-write. Returns False if the session
    does not exist.
    """
    async with _lock_for(session_id):
        return await asyncio.get_event_loop().run_in_executor(
            None, _append_message_sync, session_id, message
        )


def _append_message_sync(session_id: str, message: dict) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT chat_history FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            # Race-safe upsert: the frontend dispatches session
            # creation fire-and-forget, so an out-of-band writer
            # (cron scheduler firing into a freshly created session,
            # for example) can land here before the session row
            # exists. Previously we dropped the message; now insert
            # a placeholder row so the message lands. The next
            # session save will fill in title / metadata.
            try:
                conn.execute(
                    "INSERT INTO sessions (id, title, custom_title, generated_title, "
                    "created_at, updated_at, segments, chat_history, speaker_names) "
                    "VALUES (?, 'New Session', 0, 0, ?, ?, '[]', '[]', '{}')",
                    (session_id, now, now),
                )
                history: list = []
            except sqlite3.IntegrityError:
                # Another writer inserted in the gap; re-read.
                row = conn.execute(
                    "SELECT chat_history FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    log.warning("append_message: session %s vanished mid-insert", session_id)
                    return False
                try:
                    history = json.loads(row["chat_history"]) or []
                except (TypeError, ValueError):
                    history = []
        else:
            try:
                history = json.loads(row["chat_history"]) or []
            except (TypeError, ValueError):
                history = []
        history.append(message)
        history = _enforce_cron_event_cap(history)
        conn.execute(
            "UPDATE sessions SET chat_history = ?, updated_at = ? WHERE id = ?",
            (json.dumps(history), now, session_id),
        )
    return True


def get_session_meta(session_id: str) -> dict | None:
    """Lightweight {title, archived} lookup, or None if the session is gone.

    Used by the cron scheduler to decide where a firing should land (owning
    session vs the Scheduled Reports inbox) and to label the settings panel.
    """
    if not session_id:
        return None
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        return None
    return {"title": row["title"], "archived": bool(_safe_col(row, "archived", 0))}


def ensure_cron_inbox() -> str:
    """Create (idempotently) the pinned Scheduled Reports inbox session and
    return its id. Cron routes orphaned/archived firings here."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE id = ?", (CRON_INBOX_ID,)).fetchone()
        if row is None:
            try:
                conn.execute(
                    "INSERT INTO sessions (id, title, custom_title, generated_title, "
                    "created_at, updated_at, segments, chat_history, speaker_names) "
                    "VALUES (?, ?, 1, 0, ?, ?, '[]', '[]', '{}')",
                    (CRON_INBOX_ID, CRON_INBOX_TITLE, now, now),
                )
            except sqlite3.IntegrityError:
                pass
    # Pin it so it floats to the top of the sidebar (column from migration 005).
    try:
        with _get_conn() as conn:
            conn.execute("UPDATE sessions SET pinned = 1 WHERE id = ?", (CRON_INBOX_ID,))
    except sqlite3.OperationalError:
        pass
    return CRON_INBOX_ID


def _upsert_session(
    session_id: str,
    *,
    title: str,
    custom_title: int,
    generated_title: int,
    created_at: str,
    updated_at: str,
    segments: str,
    chat_history_frontend: list,
    speaker_names: str,
    workspace_path: str,
    compaction_count: int,
    latched_config: str,
) -> None:
    """UPSERT a session, merging backend-owned UI-only chat_history rows
    (e.g. cron_event entries appended by the cron daemon) into the
    frontend's submitted chat history so they survive the save.
    """
    with _get_conn() as conn:
        existing_row = conn.execute(
            "SELECT chat_history FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        merged_history = chat_history_frontend
        if existing_row is not None:
            try:
                existing = json.loads(existing_row["chat_history"]) or []
            except (TypeError, ValueError):
                existing = []
            backend_owned = _ui_only_rows(existing)
            if backend_owned:
                # Append all backend-owned rows that aren't already in
                # the frontend payload (frontend will start including
                # them once it re-hydrates).
                seen_ts = {
                    m.get("timestamp")
                    for m in chat_history_frontend
                    if m.get("role") not in PROMPT_ROLES and m.get("timestamp")
                }
                merged_history = list(chat_history_frontend) + [
                    m for m in backend_owned if m.get("timestamp") not in seen_ts
                ]
        merged_history = _enforce_cron_event_cap(merged_history)
        conn.execute(
            """
            INSERT INTO sessions (id, title, custom_title, generated_title, created_at, updated_at,
                                  segments, chat_history, speaker_names,
                                  workspace_path, compaction_count, latched_config)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                custom_title=excluded.custom_title,
                generated_title=excluded.generated_title,
                updated_at=excluded.updated_at,
                segments=excluded.segments,
                chat_history=excluded.chat_history,
                speaker_names=excluded.speaker_names,
                workspace_path=excluded.workspace_path,
                compaction_count=excluded.compaction_count,
                latched_config=excluded.latched_config
            """,
            (
                session_id,
                title,
                custom_title,
                generated_title,
                created_at,
                updated_at,
                segments,
                json.dumps(merged_history),
                speaker_names,
                workspace_path,
                compaction_count,
                latched_config,
            ),
        )


def _delete_session_sync(session_id: str) -> None:
    """Remove every trace of a session: the parent row, all child rows,
    the on-disk speaker profile, and the per-session in-memory caches.

    The parent ``sessions`` row and ``session_costs`` are deleted here;
    ``clear_session_tasks`` owns the ``tasks`` table (+ its cache). The
    remaining helpers each clear one in-memory cache. All are best-effort
    and self-guarding, so one failure can't block the rest. Imports are
    function-local to avoid import cycles (same pattern as get_session).
    """
    from server import cwd_tracker, diarization, file_state, shell_snapshot
    from server.infrastructure.config import unlatch_session
    from server.memory import extract as memory_extract
    from server.memory import session_memory
    from server.tasks_tracker import clear_session_tasks

    with _get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        try:
            conn.execute("DELETE FROM session_costs WHERE session_id = ?", (session_id,))
        except sqlite3.OperationalError:
            # session_costs comes from migration 001 (app startup). A database
            # that has only seen _ensure_db lacks the table — and has no cost
            # rows to delete. Same tolerance as ensure_cron_inbox's pinned UPDATE.
            pass

    # Cron jobs owned by this session must not silently die: disable + flag
    # them orphaned (re-homeable) and repoint their run history to the inbox
    # so nothing is lost. Function-local import avoids an import cycle.
    try:
        from server.cron_scheduler import on_session_deleted

        on_session_deleted(session_id)
    except Exception:
        log.warning(
            "session delete: cron cascade failed for %s",
            session_id,
            exc_info=True,
        )

    for fn in (
        clear_session_tasks,
        diarization.drop_session,
        shell_snapshot.clear_session,
        cwd_tracker.clear_session,
        file_state.clear_session,
        unlatch_session,  # drops the latched config snapshot
        session_memory.drop_session,  # drops the update-cadence state
        memory_extract.drop_session,  # drops extraction cursor/throttle state
    ):
        try:
            fn(session_id)
        except Exception:
            log.warning(
                "session delete: %s cleanup failed for %s",
                getattr(fn, "__name__", fn),
                session_id,
                exc_info=True,
            )


# HTTP handlers live in the sibling routes module; importing it registers them
# on ``router`` via decorator side-effects. Imported here (after router and the
# data-access helpers it depends on are defined) so ``from
# server.infrastructure.sessions import router`` yields a fully-wired router.
from server.infrastructure import sessions_routes  # noqa: E402,F401

_ensure_db()
