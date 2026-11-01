"""Chat package: the /api/chat routes and the turn machinery behind them.

The package binds two shared names before it imports any submodule:

- ``router``: the APIRouter every ``@router.*`` decorator in the package
  registers on (server/main.py mounts it).
- ``executor``: the ThreadPoolExecutor for blocking Bedrock calls wrapped
  in ``loop.run_in_executor``. Submodules read it with
  ``from server.chat import executor``.

Everything else is imported from its own submodule (``server.chat.infra``,
``server.chat.compaction``, ``server.chat.tool_pool`` and so on).
"""

from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter

# Bound BEFORE the submodule imports below: routes.py reads both at import.
router = APIRouter(tags=["chat"])
executor = ThreadPoolExecutor(max_workers=8)

# routes registers the HTTP handlers on ``router``. tool_pool imports every
# tool module, so a broken tool module fails at startup, not on the first turn.
from . import routes, tool_pool  # noqa: E402,F401
