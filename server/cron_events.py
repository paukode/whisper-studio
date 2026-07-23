"""Cron run progress events and cooperative stop.

Before this module a running cron job was a black box: the user saw a polled
"Running…" chip and then, minutes later, the final card — no turns, no tool
calls, no way to stop it. Now `cron_run.py` emits TeamProgressEvent-shaped
frames (phase started/turn_start/tool_call/tool_result/completed/failed/
stopped) on the agents event bus under ``type: "cron_progress"``; the
long-lived session SSE forwards them as ``team_progress``, so the existing
TeamReportCard/AgentCard fold renders a live per-tool log with zero new
frontend card code.

Stop is cooperative: `request_stop` sets a per-job threading.Event that the
run loop checks at round boundaries and between sequential tools, and the
tool-future wait polls in 1s slices so an in-flight tool is abandoned within
about a second of the request.
"""

import logging
import threading

log = logging.getLogger("whisper-studio")

_lock = threading.Lock()
_stop_events: dict[str, threading.Event] = {}


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
    run folds into a single card row. Safe from cron's daemon thread (the
    event bus routes cross-thread publishes through the subscriber's loop).
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


def open_run(job_id: str) -> None:
    """Register a fresh stop flag for a starting run."""
    with _lock:
        _stop_events[job_id] = threading.Event()


def close_run(job_id: str) -> None:
    with _lock:
        _stop_events.pop(job_id, None)


def request_stop(job_id: str) -> bool:
    """Signal a running job to stop; False when no run is registered."""
    with _lock:
        ev = _stop_events.get(job_id)
    if ev is None:
        return False
    ev.set()
    return True


def stop_requested(job_id: str) -> bool:
    with _lock:
        ev = _stop_events.get(job_id)
    return ev.is_set() if ev is not None else False
