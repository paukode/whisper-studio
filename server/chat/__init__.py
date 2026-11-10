"""Chat package: the /api/chat routes and the turn machinery behind them.

The package binds two shared names before it imports any submodule:

- ``router``: the APIRouter every ``@router.*`` decorator in the package
  registers on (server/main.py mounts it).
- ``executor``: the ThreadPoolExecutor for the blocking tool calls of every
  chat session (``ws_run_command``, ``web_fetch`` and the other sync
  executors hold a worker for their whole run). Model calls never use it:
  each one runs on a thread of its own (server/infrastructure/own_thread.py).
  Submodules read it with ``from server.chat import executor``.

Everything else is imported from its own submodule (``server.chat.infra``,
``server.chat.compaction``, ``server.chat.tool_pool`` and so on).
"""

from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter

# Bound BEFORE the submodule imports below: routes.py reads both at import.
router = APIRouter(tags=["chat"])
# Four workers for each of the 8 sessions the UI lets run at once
# (MAX_ACTIVE_SESSIONS in src/hooks/useChatStream.ts), so a parallel batch of
# reads in one session does not hold up the tools of the others. Workers start
# only when a call needs one.
executor = ThreadPoolExecutor(max_workers=32)

# routes registers the HTTP handlers on ``router``. tool_pool imports every
# tool module, so a broken tool module fails at startup, not on the first turn.
from . import routes, tool_pool  # noqa: E402,F401
