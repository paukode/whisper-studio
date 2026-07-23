"""
Task tracking system for Whisper Studio.
Allows Claude to decompose complex requests into steps and track progress.
Tasks are session-scoped, persisted to SQLite, and cached in memory.
"""

import json
import logging
import os
import sqlite3
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# In-memory cache, keyed by session_id
_task_store: dict[str, list[dict]] = {}

TASK_STATUSES = {"pending", "in_progress", "completed", "deleted"}

# Use the same DB as sessions
_STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "storage")
_DB_PATH = os.path.join(_STORAGE_DIR, "sessions.db")


def _get_conn():
    os.makedirs(_STORAGE_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at REAL,
            updated_at REAL,
            PRIMARY KEY (id, session_id)
        )
    """)
    conn.commit()
    return conn


def _load_from_db(session_id: str) -> list[dict]:
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE session_id = ? ORDER BY created_at ASC", (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error("Failed to load tasks from DB: %s", e)
        return []


def _save_task_to_db(task: dict, session_id: str):
    try:
        with _get_conn() as conn:
            conn.execute(
                """
                INSERT INTO tasks (id, session_id, subject, description, status, created_at, updated_at)
                VALUES (:id, :session_id, :subject, :description, :status, :created_at, :updated_at)
                ON CONFLICT(id, session_id) DO UPDATE SET
                    subject = excluded.subject,
                    description = excluded.description,
                    status = excluded.status,
                    updated_at = excluded.updated_at
            """,
                {**task, "session_id": session_id},
            )
            conn.commit()
    except Exception as e:
        log.error("Failed to save task to DB: %s", e)


def _reconcile_in_progress(session_id: str, prefer_id: str | None = None) -> bool:
    """Enforce the single-active invariant: at most one task may be
    ``in_progress`` per session.

    A turn interrupted mid-task (e.g. a page refresh) leaves its task frozen
    as ``in_progress``; the next turn may start another, so the UI ends up with
    two spinners. This demotes the extra ``in_progress`` tasks back to
    ``pending`` and persists the change. Which task stays active:

    - ``prefer_id`` if given and currently in_progress (the task the model just
      started this turn), otherwise
    - the most-recently-updated in_progress task, so a live turn wins over a
      zombie left behind by an interrupted one.

    Returns ``True`` if anything was demoted.
    """
    tasks = _task_store.get(session_id, [])
    active = [t for t in tasks if t.get("status") == "in_progress"]
    if len(active) <= 1:
        return False
    if prefer_id and any(t["id"] == prefer_id for t in active):
        keep_id = prefer_id
    else:
        keep_id = max(active, key=lambda t: t.get("updated_at") or 0)["id"]
    changed = False
    for task in active:
        if task["id"] != keep_id:
            task["status"] = "pending"
            task["updated_at"] = time.time()
            _save_task_to_db(task, session_id)
            changed = True
    return changed


def get_session_tasks(session_id: str) -> list[dict]:
    if session_id not in _task_store:
        _task_store[session_id] = _load_from_db(session_id)
    # Self-heal: never expose more than one in_progress task. This runs on every
    # read (live SSE broadcast, restore-on-resume, prompt build), so a session
    # left with a zombie in_progress from an interrupted turn is repaired the
    # next time its tasks are loaded.
    _reconcile_in_progress(session_id)
    return [t for t in _task_store[session_id] if t.get("status") != "deleted"]


def create_task(session_id: str, subject: str, description: str = "") -> dict:
    if session_id not in _task_store:
        _task_store[session_id] = _load_from_db(session_id)
    task = {
        "id": str(uuid.uuid4())[:8],
        "subject": subject,
        "description": description,
        "status": "pending",
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    _task_store[session_id].append(task)
    _save_task_to_db(task, session_id)
    return task


def update_task(
    session_id: str, task_id: str, status: str = None, subject: str = None
) -> dict | None:
    if session_id not in _task_store:
        _task_store[session_id] = _load_from_db(session_id)
    for task in _task_store[session_id]:
        if task["id"] == task_id:
            # A status that was explicitly provided but isn't recognised is a
            # real input error, not a silent no-op. Bail out (return None)
            # BEFORE bumping updated_at / persisting, so the caller reports the
            # bad input instead of a false success on an unchanged task.
            if status is not None and status not in TASK_STATUSES:
                return None
            if status:
                task["status"] = status
            if subject:
                task["subject"] = subject
            task["updated_at"] = time.time()
            _save_task_to_db(task, session_id)
            # Starting a task demotes any other in_progress task, so the plan
            # always has a single active step.
            if task["status"] == "in_progress":
                _reconcile_in_progress(session_id, prefer_id=task_id)
            return task
    return None


def clear_session_tasks(session_id: str):
    _task_store.pop(session_id, None)
    try:
        with _get_conn() as conn:
            conn.execute("DELETE FROM tasks WHERE session_id = ?", (session_id,))
            conn.commit()
    except Exception as e:
        log.error("Failed to clear tasks from DB: %s", e)


# Tools exposed to Claude
TASK_TOOLS = [
    {
        "name": "task_create",
        "description": (
            "Create a new task to track a step in a multi-step workflow. "
            "Use this to decompose complex requests into trackable steps visible to the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "Short task title in imperative form (e.g. 'Read config file')",
                },
                "description": {
                    "type": "string",
                    "description": "Detailed description of what needs to be done",
                },
                "session_id": {
                    "type": "string",
                    "description": "Current session ID for task scoping",
                },
            },
            "required": ["subject", "session_id"],
        },
    },
    {
        "name": "task_update",
        "description": "Update a task's status. Mark tasks in_progress when starting, completed when done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID to update"},
                "status": {
                    "type": "string",
                    "description": "New status",
                    "enum": ["pending", "in_progress", "completed"],
                },
                "session_id": {"type": "string", "description": "Current session ID"},
            },
            "required": ["task_id", "status", "session_id"],
        },
    },
    {
        "name": "task_list",
        "description": "List all tasks in the current session to review progress.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Current session ID"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "task_get",
        "description": "Get full details of a specific task by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID to retrieve"},
                "session_id": {"type": "string", "description": "Current session ID"},
            },
            "required": ["task_id", "session_id"],
        },
    },
    {
        "name": "task_stop",
        "description": "Cancel and remove a task that is no longer needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID to cancel"},
                "session_id": {"type": "string", "description": "Current session ID"},
            },
            "required": ["task_id", "session_id"],
        },
    },
]


def execute_task_tool(tool_name: str, tool_input: dict) -> str:
    session_id = tool_input.get("session_id", "default")
    if tool_name == "task_create":
        task = create_task(
            session_id, tool_input.get("subject", ""), tool_input.get("description", "")
        )
        return json.dumps({"created": True, "task_id": task["id"], "task": task})
    elif tool_name == "task_update":
        status = tool_input.get("status")
        # Reject an out-of-range status with an explicit message rather than
        # letting update_task return None (which reads as "Task not found").
        # Status is user-visible-facing only via task_stop → "deleted"; the
        # model is expected to send pending/in_progress/completed.
        if status is not None and status not in TASK_STATUSES:
            return json.dumps(
                {"error": f"Invalid status {status!r}. Must be one of: {sorted(TASK_STATUSES)}"}
            )
        task = update_task(session_id, tool_input.get("task_id", ""), status)
        if task:
            return json.dumps({"updated": True, "task": task})
        return json.dumps({"error": "Task not found"})
    elif tool_name == "task_list":
        tasks = get_session_tasks(session_id)
        return json.dumps({"tasks": tasks, "total": len(tasks)})
    elif tool_name == "task_get":
        task_id = tool_input.get("task_id", "")
        if session_id not in _task_store:
            _task_store[session_id] = _load_from_db(session_id)
        task = next((t for t in _task_store[session_id] if t["id"] == task_id), None)
        if task:
            return json.dumps({"task": task})
        return json.dumps({"error": f"Task {task_id} not found"})
    elif tool_name == "task_stop":
        task_id = tool_input.get("task_id", "")
        task = update_task(session_id, task_id, status="deleted")
        if task:
            return json.dumps({"stopped": True, "task_id": task_id})
        return json.dumps({"error": f"Task {task_id} not found"})
    return json.dumps({"error": f"Unknown task tool: {tool_name}"})


# --- API Routes ---


@router.get("/{session_id}")
async def get_tasks(session_id: str):
    return {"tasks": get_session_tasks(session_id)}


@router.post("/{session_id}")
async def api_create_task(session_id: str, request: Request):
    body = await request.json()
    subject = body.get("subject", "").strip()
    description = body.get("description", "")
    if not subject:
        return Response(
            content=json.dumps({"error": "subject required"}),
            status_code=400,
            media_type="application/json",
        )
    task = create_task(session_id, subject, description)
    return {"task": task}


@router.delete("/{session_id}")
async def api_clear_tasks(session_id: str, confirm: bool = False):
    """Clear all tasks for a session. Requires explicit ``confirm=true``
    in the query string — without it, an auto-retry from a stale tab
    could silently wipe out an active session's task list. No frontend
    code calls this today; it exists for deliberate, manual clears.
    """
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm=true required to clear session tasks",
        )
    clear_session_tasks(session_id)
    return {"cleared": True}
