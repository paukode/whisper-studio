"""Detached agent runs as registry tasks.

``start_detached_agent`` launches ``run_agent`` fire-and-forget on the server
loop: the caller gets a task_id immediately, progress events stream to a
private channel (mirrored into the owning session as ``team_progress`` and
appended to the task's output file so ``task_output`` works uniformly across
kinds), and completion lands in the session as a ``task_event`` card.

``register_external_task``/``finish_external_task`` are the substrate hooks a
future workflow runtime consumes to inherit persistence, events, stop, and UI
for free.
"""

import asyncio
import logging

from server.tasks import registry, shell
from server.tasks.events import emit_task_event

log = logging.getLogger("whisper-studio")

# Cap detached-agent concurrency so fire-and-forget spawns cannot starve the
# global agent thread pool that interactive spawn_agent shares.
DETACHED_CONCURRENCY = 2
_sem: asyncio.Semaphore | None = None

# task_id -> asyncio.Task, for cancellation. In-memory by nature (a restart
# kills the coroutine; boot reconcile marks the row interrupted).
_running: dict[str, asyncio.Task] = {}


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(DETACHED_CONCURRENCY)
    return _sem


def start_detached_agent(
    task: str,
    *,
    agent_type: str = "general",
    session_id: str = "",
    model_id: str | None = None,
    effort_label: str | None = None,
    read_only: bool = False,
    isolation: str = "none",
) -> str:
    """Launch an agent in the background; returns its registry task_id.

    Must be called from the server event loop (route handlers, tool dispatch).
    """
    task_id = registry.create_task(
        "agent",
        session_id=session_id,
        title=task,
        meta={"agent_type": agent_type, "model": model_id or ""},
    )
    out_path = shell.output_path_for(task_id)
    with registry._get_conn() as conn:
        conn.execute("UPDATE agent_tasks SET output_path=? WHERE task_id=?", (out_path, task_id))

    aio_task = asyncio.create_task(
        _run_detached(
            task_id,
            task,
            agent_type,
            session_id,
            model_id,
            out_path,
            effort_label,
            read_only,
            isolation,
        ),
        name=f"detached-agent-{task_id}",
    )
    _running[task_id] = aio_task
    aio_task.add_done_callback(lambda _t: _running.pop(task_id, None))
    started = registry.get_task(task_id)
    if started:
        emit_task_event(session_id, "task_started", started)
    return task_id


async def _run_detached(
    task_id: str,
    task: str,
    agent_type: str,
    session_id: str,
    model_id: str | None,
    out_path: str,
    effort_label: str | None = None,
    read_only: bool = False,
    isolation: str = "none",
) -> None:
    from server.agents.event_bus import event_bus
    from server.agents.runtime import run_agent

    config = None
    if read_only:
        # No human is present to approve writes and agents auto-approve the
        # [WS_APPROVAL] gate, so detached runs get the read-only tool filter.
        # Resolve through get_agent_config (NOT the raw AGENT_TYPES table) so
        # config.json agent_limits overrides apply to detached runs too —
        # run_agent only applies them itself when config is None.
        import dataclasses

        from server.agents.config import get_agent_config

        config = dataclasses.replace(get_agent_config(agent_type), read_only=True)

    channel = f"task-events:{task_id}"
    queue = event_bus.subscribe(channel)
    pump = asyncio.create_task(_pump_events(queue, session_id, task_id, out_path))
    status = "failed"
    result_text = ""
    try:
        async with _semaphore():
            result = await run_agent(
                task,
                agent_type=agent_type,
                config=config,
                session_id=session_id,
                model_id_override=model_id,
                event_channel=channel,
                effort_label=effort_label,
                isolation=isolation,
            )
        result_text = (result.output or "").strip()
        status = {"completed": "completed", "stopped": "stopped"}.get(result.status, "failed")
    except asyncio.CancelledError:
        status = "stopped"
        result_text = "[Stopped by user]"
    except Exception as e:
        status = "failed"
        result_text = f"[Agent Error] {e}"
        log.error("detached agent %s failed: %s", task_id, e)
    finally:
        # Orderly pump shutdown: cancelling immediately dropped whatever was
        # still queued — including the agent's FINAL completed/turn_limit event,
        # so the output file ended mid-run. Publish a stop sentinel so the pump
        # drains everything ahead of it in order, then falls out of its loop;
        # cancel only as a backstop if it doesn't finish promptly.
        event_bus.publish(channel, _PUMP_STOP)
        try:
            await asyncio.wait_for(pump, timeout=5)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — backstop only
            pump.cancel()
        event_bus.unsubscribe(channel, queue)
        finished = registry.finish_task(
            task_id, status=status, exit_code=None, result_text=result_text
        )
        if finished:
            event_name = {
                "completed": "task_completed",
                "stopped": "task_stopped",
                "failed": "task_failed",
            }[status]
            emit_task_event(session_id, event_name, finished)


# Sentinel published to a detached agent's private channel after its run
# returns; the pump drains everything queued before it, then exits cleanly.
_PUMP_STOP: dict = {"__pump_stop__": True}


async def _pump_events(queue: asyncio.Queue, session_id: str, task_id: str, out_path: str) -> None:
    """Mirror private-channel agent events into the session bus and the
    task output file (so ``task_output`` shows live progress for agents too)."""
    from server.agents.event_bus import event_bus

    try:
        while True:
            ev = await queue.get()
            if ev is _PUMP_STOP or (isinstance(ev, dict) and ev.get("__pump_stop__")):
                return
            line = _event_line(ev)
            if line:
                try:
                    with open(out_path, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError:
                    pass
            if session_id:
                event_bus.publish(session_id, {**ev, "detached_task_id": task_id})
    except asyncio.CancelledError:
        return


def _event_line(ev: dict) -> str:
    """Render one agent progress event as an output-file line.

    Reads the keys the agent runtime actually emits (``phase`` +
    ``tool_name``/``tool_input_preview``/``output_preview``/``text``). The
    previous version looked for ``event``/``preview``/``tool`` keys that
    ``server/agents/runtime.py`` never sets, so detached agents wrote an EMPTY
    output file and ``task_output`` showed no tool activity."""
    phase = ev.get("phase") or ev.get("event") or ev.get("type") or ""
    if phase not in (
        "text",
        "tool_call",
        "tool_result",
        "turn_start",
        "completed",
        "turn_limit",
        "failed",
        "stopped",
    ):
        return ""
    if phase == "tool_call":
        detail = f"{ev.get('tool_name', '')} {ev.get('tool_input_preview', '')}".strip()
    elif phase == "tool_result":
        status = ev.get("status")
        prefix = "ERROR " if status == "error" else ""
        detail = f"{prefix}{ev.get('tool_name', '')} {ev.get('output_preview', '')}".strip()
    elif phase == "text":
        detail = ev.get("text", "")
    elif phase == "turn_start":
        detail = f"turn {ev.get('turn', '')}"
    elif phase in ("completed", "turn_limit"):
        detail = f"turns_used={ev.get('turns_used', '')}"
    elif phase == "stopped":
        detail = "stopped by user"
    else:  # failed
        detail = ev.get("error") or ev.get("text") or ""
    if isinstance(detail, dict):
        detail = str(detail)
    return f"[{phase}] {str(detail)[:500]}".rstrip()


def cancel_task(task_id: str) -> bool:
    """Cancel a running detached agent/workflow coroutine."""
    aio_task = _running.get(task_id)
    if aio_task is None or aio_task.done():
        return False
    aio_task.cancel()
    return True


def register_external_task(
    kind: str,
    title: str,
    *,
    session_id: str = "",
    meta: dict | None = None,
    aio_task: asyncio.Task | None = None,
) -> str:
    """Registry + events + cancel wiring for externally-managed work (the
    workflow runtime lands on this hook)."""
    task_id = registry.create_task(kind, session_id=session_id, title=title, meta=meta)
    if aio_task is not None:
        _running[task_id] = aio_task
        aio_task.add_done_callback(lambda _t: _running.pop(task_id, None))
    started = registry.get_task(task_id)
    if started:
        emit_task_event(session_id, "task_started", started)
    return task_id


def finish_external_task(task_id: str, *, status: str, result_text: str = "") -> None:
    task = registry.get_task(task_id)
    if not task:
        return
    finished = registry.finish_task(task_id, status=status, result_text=result_text)
    if finished:
        event_name = {
            "completed": "task_completed",
            "stopped": "task_stopped",
            "failed": "task_failed",
            "interrupted": "task_failed",
        }.get(status, "task_completed")
        emit_task_event(task.get("session_id", ""), event_name, finished)
