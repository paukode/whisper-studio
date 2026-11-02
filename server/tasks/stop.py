"""Stop one running background-task row, whatever its kind.

The header's Stop (server/tasks/routes.py) and the model's task_cancel tool
(server/tasks/tools.py) share this dispatch: a shell command by its process
group, an agent by its coroutine or its team, a workflow by its run, a CI
watch by its cancel flag.
"""

import asyncio


async def stop_row(task: dict) -> bool:
    """Send the stop for ``task`` (a registry row) and say whether it was sent."""
    kind = task["kind"]
    meta = task.get("meta") or {}
    if kind == "shell":
        from server.tasks.shell import stop_task

        # kill_process_group blocks; keep it off the event loop.
        return await asyncio.to_thread(stop_task, task["task_id"])
    if kind == "workflow":
        from server.workflows.manager import stop_run

        return await stop_run(str(meta.get("run_id") or ""))
    if kind == "ci":
        from server.ci.manager import stop_watch

        return stop_watch(task["task_id"])
    from server.tasks.agents import cancel_task

    return cancel_task(task["task_id"])


def stop_row_from_thread(task: dict, timeout: float = 60.0) -> bool:
    """stop_row for a tool executor running on a worker thread: the stop runs
    on the server loop, where the workflow, CI and agent coroutines live (a
    shell stop is a plain process-group kill and runs right here)."""
    if task["kind"] == "shell":
        from server.tasks.shell import stop_task

        return stop_task(task["task_id"])
    from server.tasks import events

    loop = events._server_loop
    if loop is None:
        raise RuntimeError("the server event loop is not running")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("stop_row_from_thread must not run on an event loop thread")
    return asyncio.run_coroutine_threadsafe(stop_row(task), loop).result(timeout)
