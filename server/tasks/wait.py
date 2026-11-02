"""Blocking waits for background work: ``task_wait`` and ``workflow_wait``.

A model that started detached agents or a workflow used to poll for them with
``sleep`` and ``task_status`` in a loop, one round per check, each round
re-billing the whole prompt. A wait tool blocks inside ONE tool call until the
work ends (or a bounded timeout passes) and then returns the status, so the
model spends no rounds while it waits.

Time limits. server/agents/tool_access.py lists both tools in
WAITS_ON_AGENTS: an agent run that waits on agents or workflows it started is
not using its own time, so that wait never counts against its time limit
(ToolScope.waited). A task_wait on a shell command is the agent's own work:
it stays on the agent's own clock (``waits_on_agents``). A run whose wait
does not count as waited (a shell wait, or a cron or headless run, which has
no ToolScope) never waits past the run's time limit: the timeout is cut to
the time the run has left (``WaitBounds.deadline``). A chat turn has no time
limit at all.

Mid-turn messages. The chat turn reads its mid-turn inbox only between
rounds. A wait in that turn returns early with ``interrupted:
"user_message"`` as soon as the user sends a message, so the next round reads
it at once instead of after the timeout. A Stop cancels the call like any
other tool.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

DEFAULT_WAIT_SECONDS = 600
MAX_WAIT_SECONDS = 1800
MAX_WAIT_IDS = 32
# How often the server re-reads the stores while a wait is open. The model
# does not poll; this loop runs inside the one tool call.
POLL_INTERVAL_SECONDS = 1.0


# Task kinds that run agents on their own limits (server/tasks/registry.py).
AGENT_KINDS = frozenset({"agent", "workflow"})


@dataclass(frozen=True)
class WaitBounds:
    """What bounds a wait in the turn that runs it, set per tool batch by
    server/chat/engine/tool_rounds.py. ``inbox_session``: the session whose
    mid-turn user message ends the wait early (only the chat turn that reads
    that inbox). ``deadline``: the monotonic time the run must start its last
    round by, or None when it has no time limit. ``scoped``: the run has a
    ToolScope, so a wait on agents counts as waited and is not cut."""

    inbox_session: str = ""
    deadline: float | None = None
    scoped: bool = False


wait_bounds: contextvars.ContextVar[WaitBounds | None] = contextvars.ContextVar(
    "wait_bounds", default=None
)
_NO_BOUNDS = WaitBounds()


def waits_on_agents(tool_name: str, tool_input: dict) -> bool:
    """Whether a wait call waits only on agents or workflows, so its time is
    child-agent waiting. A workflow_wait always is. A task_wait is when every
    id it names is a task of kind agent or workflow; a shell command, an
    unknown id or no id at all keeps the time on the caller's own clock."""
    if tool_name == "workflow_wait":
        return True
    if tool_name != "task_wait":
        return False
    ids = wait_ids((tool_input or {}).get("task_ids"))
    if not ids:
        return False
    from server.tasks import registry

    for task_id in ids:
        task = registry.get_task(task_id)
        if not task or task.get("kind") not in AGENT_KINDS:
            return False
    return True


def bounded_timeout(timeout: float, counted: bool, bounds: WaitBounds | None = None) -> float:
    """The timeout cut to the time the run has left, unless the wait counts as
    child-agent waiting in a scoped run (that time extends the run's deadline
    instead)."""
    bounds = bounds or wait_bounds.get() or _NO_BOUNDS
    if bounds.deadline is None or (counted and bounds.scoped):
        return timeout
    return max(0.0, min(timeout, bounds.deadline - time.monotonic()))


def user_message_waiting(bounds: WaitBounds | None = None) -> Callable[[], bool] | None:
    """A check for a user message in the chat turn's mid-turn inbox, or None
    when the turn does not read that inbox."""
    bounds = bounds or wait_bounds.get() or _NO_BOUNDS
    if not bounds.inbox_session:
        return None
    from server.chat.engine.midturn_inbox import has_user_message

    session = bounds.inbox_session
    return lambda: has_user_message(session)


def wait_seconds(raw) -> float:
    """The bounded timeout of one wait call."""
    if isinstance(raw, bool):
        return float(DEFAULT_WAIT_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_WAIT_SECONDS)
    if value <= 0:
        return float(DEFAULT_WAIT_SECONDS)
    return min(value, float(MAX_WAIT_SECONDS))


def wait_ids(raw) -> list[str]:
    """The distinct, non-empty ids of a wait call, in the order given."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out[:MAX_WAIT_IDS]


async def wait_for(
    ids: list[str],
    view: Callable[[str], dict],
    *,
    mode: str = "all",
    timeout: float = DEFAULT_WAIT_SECONDS,
    interrupted: Callable[[], bool] | None = None,
) -> dict:
    """Wait until every id (mode "all") or the first id (mode "any") is no
    longer running. ``view(id)`` returns a dict with a ``status`` key and a
    ``finished`` flag; an unknown id is finished (``status: "not_found"``) so a
    typo never holds a wait open. ``interrupted()`` True ends the wait early
    (a user message reached the chat turn). Returns the views and whether the
    wait timed out."""
    want_any = mode == "any"
    deadline = time.monotonic() + timeout
    while True:
        views = [view(i) for i in ids]
        done = [v for v in views if v.get("finished")]
        if (want_any and done) or len(done) == len(views):
            return {"timed_out": False, "mode": "any" if want_any else "all", "items": views}
        if interrupted is not None and interrupted():
            return {
                "timed_out": False,
                "interrupted": "user_message",
                "mode": "any" if want_any else "all",
                "items": views,
                "hint": (
                    "The user sent a message while you waited. Read it and answer "
                    "it first. The work still runs; wait again later if needed."
                ),
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "timed_out": True,
                "mode": "any" if want_any else "all",
                "items": views,
                "hint": (
                    "Not everything has finished yet. Call the wait again to keep "
                    "waiting, do other work, or end the turn: completion wakes you."
                ),
            }
        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))


def _task_view(task_id: str, session_id: str) -> dict:
    from server.tasks import registry
    from server.tasks.tools import _public_view

    task = registry.get_task(task_id)
    if not task:
        return {"task_id": task_id, "status": "not_found", "finished": True}
    view = _public_view(task)
    view["finished"] = task.get("status") != "running"
    owner = task.get("session_id") or ""
    # Only this session's own tasks show their result: the same containment
    # task_output applies.
    if view["finished"] and task.get("result_text") and (not owner or owner == session_id):
        view["result_tail"] = task["result_text"][-1500:]
    return view


async def execute_task_wait(tool_input: dict, session_id: str) -> str:
    ids = wait_ids(tool_input.get("task_ids"))
    if not ids:
        return "Error: task_ids must list at least one background task id."
    counted = waits_on_agents("task_wait", tool_input)
    result = await wait_for(
        ids,
        lambda i: _task_view(i, session_id),
        mode=str(tool_input.get("mode") or "all"),
        timeout=bounded_timeout(wait_seconds(tool_input.get("timeout_seconds")), counted),
        interrupted=user_message_waiting(),
    )
    return json.dumps(result, indent=2)
