"""Session-event service: persist a chat row AND publish it live.

Generalizes the proven cron-event delivery path (previously inlined in
server/cron_scheduler._emit_cron_event): a background-flavoured event is
persisted into the owning session's chat_history (so it survives with no SSE
subscriber and appears on the next hydrate) and simultaneously published on the
agents event bus (so an open ``/api/sessions/{id}/events`` stream renders it
immediately). Safe to call from any thread; no-ops the persist if the server
loop hasn't been captured yet.

Roles carried today: ``cron_event`` (via cron_scheduler's delegate) and
``task_event`` (unified background-task lifecycle). Both are UI-only roles —
``PROMPT_ROLES`` in server/infrastructure/sessions.py keeps them out of model
context automatically.
"""

import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger("whisper-studio")

_server_loop: asyncio.AbstractEventLoop | None = None

RESULT_TAIL_MAX = 2048


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def init_task_service() -> None:
    """Capture the server event loop; call once from the app lifespan."""
    global _server_loop
    _server_loop = asyncio.get_running_loop()


def emit_session_event(session_id: str, *, role: str, payload_key: str, payload: dict) -> None:
    """Persist ``{role, content:"", timestamp, <payload_key>: payload}`` into
    the session's chat_history and publish ``{type: role, <payload_key>:
    payload}`` on the event bus.

    The two deliveries are independent best-effort: a persist failure never
    blocks the live publish and vice versa.
    """
    if not session_id:
        return

    timestamp = payload.get("timestamp") or _utc_now_iso()
    persisted_row = {
        "role": role,
        "content": "",
        "timestamp": timestamp,
        payload_key: payload,
    }

    loop = _server_loop
    if loop is None:
        # Called from the loop thread before init_task_service ran (tests,
        # early startup): the running loop is the server loop.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
    if loop is None:
        log.warning("session event (%s): server loop not captured; skipping persist", role)
    else:
        try:
            from server.infrastructure.sessions import append_message

            future = asyncio.run_coroutine_threadsafe(
                append_message(session_id, persisted_row), loop
            )

            def _log_err(f):
                try:
                    if not f.result():
                        log.warning(
                            "session event (%s): session %s not found, persist skipped",
                            role,
                            session_id,
                        )
                except Exception as exc:
                    log.warning("session event (%s): append_message failed: %s", role, exc)

            future.add_done_callback(_log_err)
        except Exception as exc:
            log.warning("session event (%s): failed to dispatch append_message: %s", role, exc)

    try:
        from server.agents.event_bus import event_bus

        event_bus.publish(session_id, {"type": role, payload_key: payload})
    except Exception as exc:
        log.warning("session event (%s): failed to publish to event_bus: %s", role, exc)


def _duration_ms(task: dict) -> int | None:
    try:
        started = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
        finished_raw = task.get("finished_at")
        finished = (
            datetime.fromisoformat(finished_raw.replace("Z", "+00:00"))
            if finished_raw
            else datetime.now(timezone.utc)
        )
        return max(0, int((finished - started).total_seconds() * 1000))
    except (KeyError, TypeError, ValueError):
        return None


def emit_task_event(session_id: str, event_type: str, task: dict) -> None:
    """Announce a background task's lifecycle into its owning session.

    ``event_type`` is one of task_started | task_completed | task_failed |
    task_stopped. ``task`` is a registry row dict (server.tasks.registry).
    """
    if event_type not in ("task_started", "task_completed", "task_failed", "task_stopped"):
        raise ValueError(f"unknown task event type: {event_type!r}")
    payload = {
        "event_type": event_type,
        "task_id": task.get("task_id", ""),
        "kind": task.get("kind", ""),
        "title": task.get("title", ""),
        "status": task.get("status", ""),
        "exit_code": task.get("exit_code"),
        "duration_ms": _duration_ms(task),
        "result_tail": (task.get("result_text") or "")[-RESULT_TAIL_MAX:],
        "timestamp": _utc_now_iso(),
    }
    emit_session_event(session_id, role="task_event", payload_key="taskEvent", payload=payload)
