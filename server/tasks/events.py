"""Session-event service: persist a chat row AND publish it live.

Generalizes the proven cron-event delivery path (previously inlined in
server/cron_scheduler._emit_cron_event): a background-flavoured event is
persisted into the owning session's chat_history (so it survives with no SSE
subscriber and appears on the next hydrate) and simultaneously published on the
agents event bus (so the open ``/api/sessions/events`` stream renders it
immediately). Safe to call from any thread; no-ops the persist if the server
loop hasn't been captured yet.

Rows carried this way: ``cron_event`` (via cron_scheduler's delegate),
``agent_report`` and ``agent_answer`` (server/agents/wake.py) and
``session_message`` (server/agent_tools/cross_session.py). ``PROMPT_ROLES`` in
server/infrastructure/sessions.py decides which of them the model reads.

Background-task lifecycle (``emit_task_event``) is the exception: it is
published live only. The task registry row is its durable record, and the
header's task list hydrates from that, so no chat row is written.
"""

import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger("whisper-studio")

_server_loop: asyncio.AbstractEventLoop | None = None

RESULT_TAIL_MAX = 2048


def _result_excerpt(kind: str, text: str) -> str:
    """The slice of a task's result the task list shows, capped at RESULT_TAIL_MAX.

    A shell task's output is a log: the END is what matters, so keep the
    tail. A workflow result is a report that LEADS with its headline (status,
    agents, cost) and the script's findings, so keep the head; taking the tail
    there showed a mid-sentence fragment with the headline cut off.
    """
    if len(text) <= RESULT_TAIL_MAX:
        return text
    if kind == "workflow":
        return text[: RESULT_TAIL_MAX - 1] + "…"
    return text[-RESULT_TAIL_MAX:]


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
    # The live frame carries the stored row's timestamp. The client keys its
    # own copy of the row by it, and the save merge (sessions.save_session)
    # drops the stored row only when the client's copy has the same one;
    # with the client's clock instead, every message was kept twice and came
    # back to the model as a new user turn after later ones.
    payload = {**payload, "timestamp": timestamp}
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
    """Announce a background task's lifecycle on the app's event channel.

    ``event_type`` is one of task_started | task_completed | task_failed |
    task_stopped. ``task`` is a registry row dict (server.tasks.registry).
    Published on the owning session's channel (the app-wide one when the task
    has no session) and never persisted: the header's task list is global and
    hydrates from the registry, the chat shows nothing for it.
    """
    if event_type not in ("task_started", "task_completed", "task_failed", "task_stopped"):
        raise ValueError(f"unknown task event type: {event_type!r}")
    meta = task.get("meta")
    payload = {
        "event_type": event_type,
        "task_id": task.get("task_id", ""),
        "kind": task.get("kind", ""),
        "title": task.get("title", ""),
        # A named agent's name, which the list shows in place of the title;
        # empty for every other task.
        "agent_name": str(meta.get("agent_name") or "") if isinstance(meta, dict) else "",
        "status": task.get("status", ""),
        "exit_code": task.get("exit_code"),
        "duration_ms": _duration_ms(task),
        "result_tail": _result_excerpt(task.get("kind", ""), task.get("result_text") or ""),
        "timestamp": _utc_now_iso(),
    }
    try:
        from server.agents.event_bus import event_bus

        event_bus.publish(session_id or "", {"type": "task_event", "taskEvent": payload})
    except Exception as exc:
        log.warning("task event (%s): failed to publish to event_bus: %s", event_type, exc)


def emit_agent_report(session_id: str, payload: dict, *, wake: bool = True) -> None:
    """Persist finished agent reports into the session when no live turn can
    carry them (the parent was cancelled, or the agent ran detached or was
    resumed). Unlike task_event this row IS shown to the model: the next turn
    reads it as a user message (server.infrastructure.sessions.
    visible_chat_history), so the parent still gets its turn to act.

    ``wake=False`` persists the row without waking the parent: the launching
    turn was stopped, and the user who stopped it gets no answer they did not
    ask for. Their next message reads the row."""
    if not session_id or not isinstance(payload, dict):
        return
    payload = {**payload, "timestamp": payload.get("timestamp") or _utc_now_iso()}
    emit_session_event(session_id, role="agent_report", payload_key="agentReport", payload=payload)
    try:
        if wake:
            from server.agents.wake import maybe_wake

            maybe_wake(session_id, payload)
    except Exception as e:  # noqa: BLE001 - the row is already persisted; the wake is extra
        log.warning("agent report wake skipped: %s", e)
    try:
        from server.notifications import record_notification

        agents = payload.get("agents") or []
        title = payload.get("team_name") or (agents[0].get("name") if agents else "") or "Agent"
        record_notification(
            session_id=session_id,
            source="agents",
            title=f"Results ready: {title}",
            message=payload.get("reason")
            or f"{len(agents)} agent report(s) landed in the session.",
        )
    except Exception as e:  # noqa: BLE001 - a missed notification never blocks the row
        log.debug("agent report notification skipped: %s", e)
