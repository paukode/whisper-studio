"""Detached agent runs as registry tasks.

``start_detached_agent`` launches ``run_agent`` fire-and-forget on the server
loop: the caller gets a task_id immediately, progress events stream to a
private channel (mirrored into the owning session as ``team_progress`` and
appended to the task's output file so ``task_output`` works uniformly across
kinds), and completion lands in the session as a ``task_event`` card.
"""

import asyncio
import logging
import os

from server.agents.journal import REPORT_CHARS
from server.tasks import registry, shell
from server.tasks.events import emit_agent_report, emit_task_event

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
            # The registry row and the on-disk journal share the agent's id.
            agent_id=task_id,
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
    *,
    agent_id: str | None = None,
    resume_agent_id: str | None = None,
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
    stop_reason = "error"
    turns_used = 0
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
                agent_id=agent_id,
                resume_agent_id=resume_agent_id,
            )
        result_text = (result.output or "").strip()
        status = {"completed": "completed", "stopped": "stopped"}.get(result.status, "failed")
        stop_reason = getattr(result, "stop_reason", status)
        turns_used = int(getattr(result, "turns_used", 0) or 0)
    except asyncio.CancelledError:
        status = "stopped"
        stop_reason = "cancelled"
        rec = _journal_record(task_id, session_id)
        result_text = rec or "[Stopped by user]"
    except Exception as e:
        status = "failed"
        stop_reason = "error"
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
        # The runtime's journal normally closes the registry row first (with
        # the report); this call is then a no-op and the row is re-read.
        finished = registry.finish_task(
            task_id, status=status, exit_code=None, result_text=result_text
        ) or registry.get_task(task_id)
        if finished:
            event_name = {
                "completed": "task_completed",
                "stopped": "task_stopped",
                "failed": "task_failed",
            }[status]
            emit_task_event(session_id, event_name, finished)
        # The launching turn did not wait for this result (detached) or has
        # long ended (resumed): deliver the report as a session row the next
        # turn reads, so the parent still gets to act on it.
        if session_id:
            emit_agent_report(
                session_id,
                {
                    "team_id": task_id,
                    "team_name": ("Resumed agent" if resume_agent_id else "Background agent")
                    + f": {task[:60]}",
                    "description": task[:500],
                    "reason": (
                        "resumed after it had stopped; this is its reply"
                        if resume_agent_id
                        else "ran in the background; the turn that launched it did not wait"
                    ),
                    "agents": [
                        {
                            "name": agent_type,
                            "agent_id": task_id,
                            "agent_type": agent_type,
                            "task": task,
                            "result": (result_text or "")[:REPORT_CHARS],
                            "status": status,
                            "stop_reason": stop_reason,
                            "turns_used": turns_used,
                        }
                    ],
                },
            )


# Sentinel published to a detached agent's private channel after its run
# returns; the pump drains everything queued before it, then exits cleanly.
_PUMP_STOP: dict = {"__pump_stop__": True}


def _journal_record(agent_id: str, session_id: str) -> str:
    """The salvage report a cancelled run left in its journal, if any."""
    try:
        from server.agents import journal as journal_mod

        rec = journal_mod.load(agent_id, session_id or None) or {}
        return (rec.get("report") or "").strip()
    except Exception:  # noqa: BLE001 - best effort
        return ""


def resume_agent(agent_id: str, message: str, *, session_id: str = "") -> str:
    """Continue a finished or interrupted agent with its stored context.

    The agent keeps its id and its on-disk record; the run is detached (the
    caller's turn does not wait) and its reply lands in the session as an
    agent report. Must be called from the server event loop.
    """
    from server.agents import journal as journal_mod

    rec = journal_mod.load(agent_id, session_id or None)
    if rec is None or not rec.get("messages"):
        raise ValueError(f"no stored record for agent {agent_id}")
    meta = rec.get("meta") or {}
    sid = session_id or str(meta.get("session_id") or "")
    agent_type = str(meta.get("agent_type") or "general")
    model_id = str(meta.get("model") or "") or None
    registry.ensure_running_task(
        agent_id,
        kind="agent",
        session_id=sid,
        title=str(meta.get("task") or message)[:200],
        output_path=os.path.join(rec["dir"], "events.jsonl"),
        meta={"agent_type": agent_type, "model": model_id or "", "journal_dir": rec["dir"]},
    )
    aio_task = asyncio.create_task(
        _run_detached(
            agent_id,
            message,
            agent_type,
            sid,
            model_id,
            None,
            None,
            False,
            "none",
            agent_id=agent_id,
            resume_agent_id=agent_id,
        ),
        name=f"resume-agent-{agent_id}",
    )
    _running[agent_id] = aio_task
    aio_task.add_done_callback(lambda _t: _running.pop(agent_id, None))
    started = registry.get_task(agent_id)
    if started:
        emit_task_event(sid, "task_started", started)
    return agent_id


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
            if line and out_path:
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
