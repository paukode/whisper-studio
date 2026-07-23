"""Detached lifecycle for a CI watch.

``start_watch`` registers a ``ci`` task (WS-A registry), schedules the watcher
coroutine detached on the server loop, and returns the task id immediately.
Progress ticks publish live on the session channel (``ci_progress``); the
terminal outcome persists as a ``ci_result`` card and closes the task. Mirrors
``server.workflows.manager`` for the schedule-from-loop-or-worker-thread dance.
"""

from __future__ import annotations

import asyncio
import json
import logging

from server.ci import provider, watcher
from server.tasks import registry

log = logging.getLogger("whisper-studio")

# Live cancel handles, keyed by task_id, so a stop request can end a watch.
_cancels: dict[str, asyncio.Event] = {}


def start_watch(branch: str, cwd: str, session_id: str = "") -> str:
    task_id = registry.create_task(
        "ci",
        session_id=session_id,
        title=f"CI watch {branch}",
        meta={"branch": branch},
    )
    cancel = asyncio.Event()
    _cancels[task_id] = cancel
    # Durable lifecycle (like every other task kind): a task_started row + the
    # background-task pill count. The terminal task_completed/failed is emitted
    # in _finish, so the outcome survives even when no SSE client is attached.
    row = registry.get_task(task_id)
    if row and session_id:
        _emit_task_event(session_id, "task_started", row)
    try:
        _schedule(_drive_watch(task_id, branch, cwd, session_id, cancel), f"ci-watch-{task_id}")
    except Exception as e:  # noqa: BLE001 — never leak the cancel/registry row on a schedule failure
        log.error("ci watch %s failed to schedule: %s", task_id, e)
        _cancels.pop(task_id, None)
        _finish(task_id, session_id, branch, {"status": "error", "error": str(e)})
    return task_id


def stop_watch(task_id: str) -> bool:
    ev = _cancels.get(task_id)
    if not ev:
        return False
    ev.set()
    return True


async def _drive_watch(
    task_id: str, branch: str, cwd: str, session_id: str, cancel: asyncio.Event
) -> None:
    outcome = {}
    try:
        outcome = await watcher.watch_branch(
            branch,
            cwd=cwd,
            cancel_event=cancel,
            on_event=lambda ev: _publish_progress(session_id, task_id, ev),
        )
    except Exception as e:  # noqa: BLE001
        log.error("ci watch %s crashed: %s", task_id, e, exc_info=True)
        outcome = {"cancelled": False, "failing": False, "status": "error", "error": str(e)}
    finally:
        _cancels.pop(task_id, None)
        _finish(task_id, session_id, branch, outcome)


def _finish(task_id: str, session_id: str, branch: str, outcome: dict) -> None:
    # A crash (except path sets status="error") or an empty outcome (the coroutine
    # was cancelled at the asyncio level before returning) is a FAILED watch, not
    # a completed one — don't record a green task over a watch that never ran.
    if outcome.get("cancelled"):
        status = "stopped"
    elif not outcome or outcome.get("status") == "error":
        status = "failed"
    else:
        status = "completed"
    # Store the FULL card payload (not just conclusion/run_id) so a page reload
    # can re-attach to the exact watched run via GET /api/ci/watch/{task_id},
    # instead of guessing with the branch's latest run.
    finished = registry.finish_task(
        task_id,
        status=status,
        result_text=json.dumps(
            {**_result_payload(task_id, branch, outcome), "task_status": status}
        ),
    )
    # Durable terminal record + pill decrement (mirrors shell/agent tasks). This
    # persists a task_event chat row, so the outcome is NOT lost when no live SSE
    # client is attached at emit time; the rich live card still flips via _emit_result.
    if finished and session_id:
        event = {"completed": "task_completed", "failed": "task_failed", "stopped": "task_stopped"}[
            status
        ]
        _emit_task_event(session_id, event, finished)
    _emit_result(session_id, task_id, branch, outcome)


def _emit_task_event(session_id: str, event_type: str, task: dict) -> None:
    try:
        from server.tasks.events import emit_task_event

        emit_task_event(session_id, event_type, task)
    except Exception as e:  # noqa: BLE001
        log.debug("ci task event emit failed: %s", e)


def _publish_progress(session_id: str, task_id: str, ev: dict) -> None:
    if not session_id:
        return
    try:
        from server.agents.event_bus import event_bus

        payload = {k: v for k, v in ev.items() if k != "type"}
        event_bus.publish(session_id, {"type": "ci_progress", "task_id": task_id, **payload})
    except Exception as e:  # noqa: BLE001
        log.debug("ci progress publish failed: %s", e)


def _result_payload(task_id: str, branch: str, outcome: dict) -> dict:
    """The terminal card payload — shared by the live ci_result event and the
    durable result_text so a reload reconstructs the exact watched run."""
    return {
        "task_id": task_id,
        "branch": branch,
        "run_id": outcome.get("run_id"),
        "status": outcome.get("status"),
        "conclusion": outcome.get("conclusion"),
        "failing": bool(outcome.get("failing")),
        "url": outcome.get("url"),
        "failed_jobs": [j.get("name") for j in outcome.get("failed_jobs", [])],
        "timed_out": bool(outcome.get("timed_out")),
        "cancelled": bool(outcome.get("cancelled")),
        "found": outcome.get("found", True),
    }


def get_watch(task_id: str) -> dict | None:
    """Re-attach state for a specific ci watch task: the exact run it followed,
    reconstructed from the registry row's result_text. None if not a ci task."""
    task = registry.get_task(task_id)
    if not task or task.get("kind") != "ci":
        return None
    st = task.get("status")
    meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
    result = {}
    if task.get("result_text"):
        try:
            result = json.loads(task["result_text"])
        except (ValueError, TypeError):
            result = {}
    return {
        "task_id": task_id,
        "status": st,
        "terminal": st != "running",
        "branch": result.get("branch") or meta.get("branch", ""),
        "run": result or None,
    }


def _emit_result(session_id: str, task_id: str, branch: str, outcome: dict) -> None:
    if not session_id:
        return
    payload = _result_payload(task_id, branch, outcome)
    # Publish-only (no persisted chat row): the CI card renders from the
    # ci_started tool side-effect and re-fetches /api/ci/status on reload, so a
    # persisted empty ci_result row would just be dead weight in the transcript.
    try:
        from server.agents.event_bus import event_bus

        event_bus.publish(session_id, {"type": "ci_result", "ciResult": payload})
    except Exception as e:  # noqa: BLE001
        log.debug("ci result publish failed: %s", e)


def status_snapshot(branch: str, cwd: str) -> dict:
    """One-shot: the latest run for a branch + its (failed) jobs, for the REST
    status endpoint and the ci_status tool. Blocking; wrap in to_thread."""
    if not provider.gh_available():
        return {"available": False, "branch": branch}
    latest = provider.latest_run(branch, cwd)
    if not latest or latest.get("run_id") is None:
        return {"available": True, "branch": branch, "run": None}
    run = provider.get_run(latest["run_id"], cwd) or latest
    return {
        "available": True,
        "branch": branch,
        "run": {
            "run_id": run.get("run_id"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "workflow": run.get("workflow"),
            "url": run.get("url"),
            "failing": provider.is_failing(run),
            "jobs": run.get("jobs", []),
            "failed_jobs": [j.get("name") for j in provider.failed_jobs(run)],
        },
    }


# ── scheduling (mirror of workflows.manager._schedule) ───────────────────────
def _schedule(coro, name: str) -> None:
    from server.infrastructure.async_tasks import spawn

    try:
        asyncio.get_running_loop()
        spawn(coro, name=name)
        return
    except RuntimeError:
        pass
    loop = _server_loop()
    if loop is None:
        try:
            asyncio.run(coro)
        except Exception as e:  # noqa: BLE001
            log.error("ci watch %s inline run failed: %s", name, e)
        return
    asyncio.run_coroutine_threadsafe(coro, loop)


def _server_loop():
    try:
        from server.tasks import events

        return events._server_loop
    except Exception:
        return None
