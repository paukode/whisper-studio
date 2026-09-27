"""Which run started a command, so a chat Stop reaches only its own work.

Detached and resumed agents, workflows, voice-delegated runs and cron jobs
all run tools under the chat session id they belong to, so the session's
foreground commands and shell tasks mix their work with the chat turn's. A
chat Stop (server.tasks.handoff.stop_turn_work) must not kill a command one
of those runs started in the same time window.

Each of those runs is started through ``run_owned``, which marks everything
its coroutine starts, down to the worker threads its tools run in (contextvars
follow asyncio tasks and the tool router's executor hop). The user's chat
turn and everything it awaits (its in-turn subagents, its approved commands)
carry no owner, and that is what a chat Stop stops.
"""

import contextvars
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")

_WORK_OWNER: contextvars.ContextVar[str] = contextvars.ContextVar("work_owner", default="")


def current() -> str:
    """The run that owns work started here, or "" for the chat turn."""
    return _WORK_OWNER.get()


async def run_owned(owner: str, aw: Awaitable[T]) -> T:
    """Await ``aw`` with every command it starts owned by ``owner``."""
    token = _WORK_OWNER.set(owner)
    try:
        return await aw
    finally:
        _WORK_OWNER.reset(token)
