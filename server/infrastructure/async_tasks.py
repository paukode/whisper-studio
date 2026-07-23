"""Tracked fire-and-forget asyncio tasks.

asyncio.create_task() results must be strongly referenced or the task can
be garbage-collected mid-flight, and exceptions only surface at GC time as
"Task exception was never retrieved". spawn() keeps a module-level
reference until the task completes and logs any exception in a
done-callback. Distinct from server/tasks/shell.py, which runs
thread-based jobs with progress tracking.
"""

import asyncio
import logging
from collections.abc import Coroutine

log = logging.getLogger("whisper-studio")

_TASKS: set[asyncio.Task] = set()


def spawn(coro: Coroutine, *, name: str | None = None) -> asyncio.Task:
    """Schedule ``coro`` as a task that survives GC and logs failures."""
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)
    task.add_done_callback(_on_done)
    return task


def _on_done(task: asyncio.Task) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "Background task %r failed: %s",
            task.get_name(),
            exc,
            exc_info=exc,
        )
