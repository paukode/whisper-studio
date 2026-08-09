"""Cron run progress events and cooperative stop.

``cron_run.py`` emits TeamProgressEvent-shaped frames
(phase started/turn_start/tool_call/tool_result/completed/failed/
stopped) on the agents event bus under ``type: "cron_progress"``; the
long-lived session SSE forwards them as ``team_progress``, so the existing
TeamReportCard/AgentCard fold renders a live per-tool log with zero new
frontend card code.

Stop: `request_stop` sets the per-job threading.Event (kept for observability
and for any caller that only wants the plain flag — e.g. `stop_requested`
below) AND, since cron runs as an asyncio.Task on the server's own event loop
rather than a worker thread, cancels that task directly via
``asyncio.Task.cancel()``. Cancellation lands at the run's next await point
(the vast majority of the loop: awaiting the model stream, a tool call, or
between rounds), which is at least as fast as the old thread's 1-second
poll slices and often effectively immediate.
"""

import asyncio
import logging
import threading

log = logging.getLogger("whisper-studio")

_lock = threading.Lock()
_stop_events: dict[str, threading.Event] = {}
# The asyncio.Task actually driving a run, keyed by job_id — registered by
# _execute_cron_prompt itself (via asyncio.current_task()) so request_stop can
# cancel it. Optional: callers/tests that only care about the cooperative
# flag can still call open_run(job_id) with no task.
_running_tasks: dict[str, asyncio.Task] = {}


def emit_progress(
    session_id: str,
    *,
    run_id: str,
    job_name: str,
    phase: str,
    **fields,
) -> None:
    """Publish one TeamProgressEvent-shaped frame for a cron run.

    ``agent_id``/``team_id`` are both ``cron:<run_id>`` so every frame of one
    run folds into a single card row.
    """
    if not session_id:
        return
    event = {
        "agent_id": f"cron:{run_id}",
        "agent_name": job_name,
        "agent_type": "cron",
        "team_id": f"cron:{run_id}",
        "parent_agent_id": None,
        "phase": phase,
        **fields,
    }
    try:
        from server.agents.event_bus import event_bus

        event_bus.publish(session_id, {"type": "cron_progress", "event": event})
    except Exception as exc:
        log.warning("cron progress: publish failed: %s", exc)


def open_run(job_id: str, task: asyncio.Task | None = None) -> None:
    """Register a fresh stop flag for a starting run, and (optionally) the
    asyncio.Task actually running it, so a stop request can cancel it."""
    with _lock:
        _stop_events[job_id] = threading.Event()
        if task is not None:
            _running_tasks[job_id] = task


def close_run(job_id: str) -> None:
    with _lock:
        _stop_events.pop(job_id, None)
        _running_tasks.pop(job_id, None)


def request_stop(job_id: str) -> bool:
    """Signal a running job to stop; False when no run is registered.

    Sets the cooperative flag (read by `stop_requested`, kept for
    observability/back-compat) and, when the run registered its own task via
    `open_run`, cancels it directly.
    """
    with _lock:
        ev = _stop_events.get(job_id)
        task = _running_tasks.get(job_id)
    if ev is None:
        return False
    ev.set()
    if task is not None and not task.done():
        task.cancel()
    return True


def stop_requested(job_id: str) -> bool:
    with _lock:
        ev = _stop_events.get(job_id)
    return ev.is_set() if ev is not None else False
