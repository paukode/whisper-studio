"""Model-facing tools for the unified background-task registry.

``task_status`` and ``task_output`` resurrect what the dead ``get_task`` never
delivered: a structured way for the model to check on background work instead
of cat-ing an output file on faith. ``task_cancel`` stops a running task.

Named ``task_cancel`` (not ``task_stop``) because ``task_stop`` belongs to the
unrelated in-conversation todo tracker (server/tasks_tracker.py); descriptions
say "background task" explicitly to keep the two families apart for the model.

``task_status``/``task_output`` are read-only so subagents keep them under the
read_only filter; ``task_cancel`` is write-classified and therefore stripped
for agents — deliberate containment.
"""

import json
import logging

from server.executors import register_executor
from server.tasks import registry

log = logging.getLogger("whisper-studio")

DEFAULT_TAIL_LINES = 200

BACKGROUND_TASK_TOOLS: list[dict] = [
    {
        "name": "task_status",
        "description": (
            "Check on background tasks (shell commands, detached agents, or "
            "workflow runs — NOT the todo list). Without task_id: lists this "
            "session's background tasks plus anything running elsewhere. With "
            "task_id: full status for that task. You will also receive a task "
            "event in the conversation when a background task finishes, so "
            "prefer doing other work over polling; when you do poll, back off "
            "(30s, then 1-2 min between checks)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Optional specific background task id",
                }
            },
        },
    },
    {
        "name": "task_output",
        "description": (
            "Read a background task's output (shell command output, or an "
            "agent task's progress log). Returns the last tail_lines lines "
            "(default 200). Large outputs are budgeted with the full text "
            "available via read_cached_result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "tail_lines": {
                    "type": "integer",
                    "description": "How many trailing lines to return (default 200)",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_cancel",
        "description": (
            "Stop a RUNNING background task (shell command, detached agent, or "
            "workflow run — NOT a todo-list item; those use task_stop). The "
            "task records status 'stopped' and a task event fires in the "
            "session."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
]

BACKGROUND_TASK_TOOL_NAMES = {t["name"] for t in BACKGROUND_TASK_TOOLS}


def _public_view(task: dict) -> dict:
    view = {
        "task_id": task["task_id"],
        "kind": task["kind"],
        "title": task["title"],
        "status": task["status"],
        "session_id": task.get("session_id", ""),
        "created_at": task.get("created_at"),
        "finished_at": task.get("finished_at"),
    }
    if task.get("exit_code") is not None:
        view["exit_code"] = task["exit_code"]
    if task.get("command"):
        view["command"] = task["command"]
    if task.get("meta"):
        view["meta"] = task["meta"]
    return view


@register_executor("task_status", read_only=True, concurrent_safe=True)
def _exec_task_status(tool_input, transcript, current_attachments):
    session_id = tool_input.pop("__session_id__", "") or tool_input.get("session_id", "")
    task_id = (tool_input.get("task_id") or "").strip()
    if task_id:
        task = registry.get_task(task_id)
        if not task:
            return f"No background task found with id '{task_id}'."
        full = _public_view(task)
        if task.get("result_text"):
            full["result_tail"] = task["result_text"][-1000:]
        return json.dumps(full, indent=2)

    mine = registry.list_tasks(session_id=session_id, limit=20) if session_id else []
    running_elsewhere = [
        t
        for t in registry.list_tasks(status="running", limit=20)
        if t.get("session_id") != session_id
    ]
    if not mine and not running_elsewhere:
        return "No background tasks for this session and nothing running elsewhere."
    out = {
        "session_tasks": [_public_view(t) for t in mine],
        "running_elsewhere": [_public_view(t) for t in running_elsewhere],
    }
    return json.dumps(out, indent=2)


def _ownership_error(task: dict, session_id: str) -> str | None:
    """Model-facing containment: a session's model may only read/cancel its
    OWN background work. Cross-session visibility stays metadata-only via
    task_status; the human panel (REST) is not restricted."""
    owner = task.get("session_id") or ""
    if owner and session_id and owner != session_id:
        return (
            f"Task {task['task_id']} belongs to another session; this session "
            "can only inspect it via task_status."
        )
    return None


@register_executor("task_output", read_only=True, concurrent_safe=True)
def _exec_task_output(tool_input, transcript, current_attachments):
    session_id = tool_input.pop("__session_id__", "")
    task_id = (tool_input.get("task_id") or "").strip()
    if not task_id:
        return "Error: task_id is required."
    task = registry.get_task(task_id)
    if not task:
        return f"No background task found with id '{task_id}'."
    denied = _ownership_error(task, session_id)
    if denied:
        return denied
    try:
        tail_lines = int(tool_input.get("tail_lines") or DEFAULT_TAIL_LINES)
    except (TypeError, ValueError):
        tail_lines = DEFAULT_TAIL_LINES
    tail_lines = max(1, min(tail_lines, 5000))

    text = registry.tail_lines_of_file(task.get("output_path"), tail_lines)
    if not text:
        text = task.get("result_text") or "(no output yet)"
    header = f"[{task['kind']} task {task_id} — {task['status']}"
    if task.get("exit_code") is not None:
        header += f", exit {task['exit_code']}"
    header += "]\n"
    return header + text


@register_executor("task_cancel", read_only=False, concurrent_safe=False)
def _exec_task_cancel(tool_input, transcript, current_attachments):
    session_id = tool_input.pop("__session_id__", "")
    task_id = (tool_input.get("task_id") or "").strip()
    if not task_id:
        return "Error: task_id is required."
    task = registry.get_task(task_id)
    if not task:
        return f"No background task found with id '{task_id}'."
    denied = _ownership_error(task, session_id)
    if denied:
        return denied
    if task["status"] != "running":
        return f"Task {task_id} is not running (status: {task['status']})."
    if task["kind"] == "shell":
        from server.tasks import shell

        ok = shell.stop_task(task_id)
    else:
        from server.tasks import agents

        ok = agents.cancel_task(task_id)
    if ok:
        return f"Stop signal sent to {task['kind']} task {task_id}."
    return f"Task {task_id} could not be stopped (it may have just finished)."


def execute_background_task_tool(tool_name: str, tool_input: dict, session_id: str) -> str:
    """Dispatch entry used by server/tool_router.py."""
    from server.executors import EXECUTORS

    payload = dict(tool_input)
    payload["__session_id__"] = session_id
    fn = EXECUTORS[tool_name]
    return fn(payload, "", [])
