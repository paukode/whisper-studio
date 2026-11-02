"""Blocking waits for background work: ``task_wait`` and ``workflow_wait``.

A model that started detached agents or a workflow used to poll for them with
``sleep`` and ``task_status`` in a loop, one round per check, each round
re-billing the whole prompt. A wait tool blocks inside ONE tool call until the
work ends (or a bounded timeout passes) and then returns the status, so the
model spends no rounds while it waits.

The time spent in a wait is child-agent waiting: server/agents/tool_access.py
lists both tools in WAITS_ON_AGENTS, so it never counts against an agent's
own time limit. A chat turn has no time limit at all. The timeout keeps one
call bounded, and a Stop cancels the call like any other tool.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

DEFAULT_WAIT_SECONDS = 600
MAX_WAIT_SECONDS = 1800
MAX_WAIT_IDS = 32
# How often the server re-reads the stores while a wait is open. The model
# does not poll; this loop runs inside the one tool call.
POLL_INTERVAL_SECONDS = 1.0


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
) -> dict:
    """Wait until every id (mode "all") or the first id (mode "any") is no
    longer running. ``view(id)`` returns a dict with a ``status`` key and a
    ``finished`` flag; an unknown id is finished (``status: "not_found"``) so a
    typo never holds a wait open. Returns the views and whether the wait
    timed out."""
    want_any = mode == "any"
    deadline = time.monotonic() + timeout
    while True:
        views = [view(i) for i in ids]
        done = [v for v in views if v.get("finished")]
        if (want_any and done) or len(done) == len(views):
            return {"timed_out": False, "mode": "any" if want_any else "all", "items": views}
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
    result = await wait_for(
        ids,
        lambda i: _task_view(i, session_id),
        mode=str(tool_input.get("mode") or "all"),
        timeout=wait_seconds(tool_input.get("timeout_seconds")),
    )
    return json.dumps(result, indent=2)
