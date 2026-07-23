"""REST surface for the unified background-task registry.

Prefix /api/background-tasks — deliberately NOT /api/tasks, which would
shadow the todo-tracker routes (server/tasks_tracker.py mounts /api/tasks/...).
Backs the global BackgroundTasksPanel and the per-card stop/output actions.
"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.tasks import registry

router = APIRouter(prefix="/api/background-tasks", tags=["background-tasks"])


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


@router.get("/{task_id}")
async def get_background_task(task_id: str):
    task = registry.get_task(task_id)
    if not task:
        return _err(404, f"no background task {task_id}")
    task.pop("owner_pid", None)
    return task


@router.get("/{task_id}/output")
async def get_background_task_output(task_id: str, tail: int = 500):
    task = registry.get_task(task_id)
    if not task:
        return _err(404, f"no background task {task_id}")
    text = registry.tail_lines_of_file(task.get("output_path"), max(1, min(tail, 5000)))
    if not text:
        text = task.get("result_text") or ""
    return {"task_id": task_id, "status": task["status"], "output": text}


@router.post("/{task_id}/stop")
async def stop_background_task(task_id: str):
    task = registry.get_task(task_id)
    if not task:
        return _err(404, f"no background task {task_id}")
    if task["status"] != "running":
        return {"stopped": False, "status": task["status"]}
    if task["kind"] == "shell":
        import asyncio

        from server.tasks.shell import stop_task

        # kill_process_group blocks; keep it off the event loop.
        ok = await asyncio.to_thread(stop_task, task_id)
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
