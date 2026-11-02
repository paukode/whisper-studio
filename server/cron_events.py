"""Cron run progress events and stop.

``cron_run.py`` emits TeamProgressEvent-shaped frames
(phase started/turn_start/tool_call/tool_result/completed/failed/
stopped) on the agents event bus under ``type: "cron_progress"``; the
long-lived session SSE forwards them as ``team_progress``, so the existing
agent-row fold shows a running cron job with zero new frontend code.

Stop: a run registers the asyncio.Task driving it (`open_run`), and
`request_stop` cancels that task. Cancellation lands at the run's next await
point: awaiting the model stream, a tool call, or between rounds.
"""

import asyncio
import logging
import threading

log = logging.getLogger("whisper-studio")

_lock = threading.Lock()
# The asyncio.Task driving a run, keyed by job_id: registered by
# _execute_cron_prompt itself (via asyncio.current_task()) so request_stop can
# cancel it.
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


def open_run(job_id: str, task: asyncio.Task) -> None:
    """Register the asyncio.Task running a starting job, so a stop request
    can cancel it."""
    with _lock:
        _running_tasks[job_id] = task


def close_run(job_id: str) -> None:
    with _lock:
        _running_tasks.pop(job_id, None)


def request_stop(job_id: str) -> bool:
    """Cancel a running job's task; False when no run is registered."""
    with _lock:
        task = _running_tasks.get(job_id)
    if task is None:
        return False
    if not task.done():
        task.cancel()
    return True
