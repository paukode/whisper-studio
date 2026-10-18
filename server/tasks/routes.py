"""REST surface for the unified background-task registry.

Prefix /api/background-tasks — deliberately NOT /api/tasks, which would
shadow the todo-tracker routes (server/tasks_tracker.py mounts /api/tasks/...).
Backs the header's task list: the rows, a row's live output tail, and Stop.
"""

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.tasks import registry

router = APIRouter(prefix="/api/background-tasks", tags=["background-tasks"])

# Lines of a task's output the list shows when a row is unfolded.
OUTPUT_TAIL_LINES = 40
OUTPUT_TAIL_LINES_MAX = 500


def _err(status_code: int, message: str) -> Response:
    return Response(
        content=json.dumps({"error": message}),
        status_code=status_code,
        media_type="application/json",
    )


@router.get("")
async def list_background_tasks(
    session_id: str | None = None, status: str | None = None, limit: int = 100
):
    tasks = registry.list_tasks(session_id=session_id, status=status, limit=limit)
    for t in tasks:
        t.pop("owner_pid", None)
    return {"tasks": tasks}


@router.get("/{task_id}/output")
async def background_task_output(task_id: str, tail_lines: int = OUTPUT_TAIL_LINES):
    """The last lines of a task's output file, for the unfolded row of a
    running task (a finished row already carries its result text)."""
    task = registry.get_task(task_id)
    if not task:
        return _err(404, f"no background task {task_id}")
    lines = max(1, min(int(tail_lines), OUTPUT_TAIL_LINES_MAX))
    text = await asyncio.to_thread(registry.tail_lines_of_file, task.get("output_path"), lines)
    return {"task_id": task_id, "status": task["status"], "text": text}


@router.post("/{task_id}/stop")
async def stop_background_task(task_id: str):
    """Stop a running row of any kind: a shell command by its process group,
    an agent by its coroutine or its team, a workflow by its run, a CI watch
    by its cancel flag."""
    task = registry.get_task(task_id)
    if not task:
        return _err(404, f"no background task {task_id}")
    if task["status"] != "running":
        return {"stopped": False, "status": task["status"]}
    kind = task["kind"]
    meta = task.get("meta") or {}
    if kind == "shell":
        from server.tasks.shell import stop_task

        # kill_process_group blocks; keep it off the event loop.
        ok = await asyncio.to_thread(stop_task, task_id)
    elif kind == "workflow":
        from server.workflows.manager import stop_run

        ok = await stop_run(str(meta.get("run_id") or ""))
    elif kind == "ci":
        from server.ci.manager import stop_watch

        ok = stop_watch(task_id)
    else:
        from server.tasks.agents import cancel_task

        ok = cancel_task(task_id)
    return {"stopped": bool(ok), "status": "stopping" if ok else task["status"]}


@router.post("/stop-session")
async def stop_session_background_tasks(request: Request):
    body = await request.json()
    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        return _err(400, "session_id required")
    import asyncio

    from server.tasks.agents import _running, cancel_task
    from server.tasks.shell import stop_session_tasks

    stopped = await asyncio.to_thread(stop_session_tasks, session_id)
    for task_id in list(_running.keys()):
        task = registry.get_task(task_id)
        if task and task.get("session_id") == session_id and cancel_task(task_id):
            stopped.append(task_id)
    return {"stopped": stopped}
