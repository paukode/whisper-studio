"""Chat package: model config, tool catalogue, compaction, budgeting, routes.

Split out of the former monolithic ``server/chat.py`` (1344 lines) into:

    infra      — Bedrock client cache + model-config accessors
    tool_pool  — three-tier tool catalogue assembly + concurrency classifier
    compaction — message-history compaction (microcompact + Claude summary)
    budget     — oversize-tool-result persistence
    routes     — @router.* HTTP handlers (5 endpoints, /api/chat included)

Why ``router`` and ``executor`` live here:

- ``router`` is shared by every ``@router.*`` decorator across the package;
  defining it in ``__init__.py`` makes the submodule import order explicit
  and dodges the "two modules race to create the singleton" risk.
- ``executor`` (ThreadPoolExecutor) is the shared thread pool used by every
  Bedrock blocking call wrapped in ``loop.run_in_executor``. Submodules
  resolve it via ``from server.chat import executor`` once this file has
  bound the name — works during ``__init__.py`` evaluation because the
  binding happens BEFORE the submodule imports below.

External importers (server/main.py, server/auto_mode.py, server/doctor.py,
server/agents/runtime.py, tests/test_chat_truncation.py)
keep reading the same names off ``server.chat`` thanks to the re-exports here.
"""

from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter

# These must be bound BEFORE the submodule imports — compaction.py and
# routes.py reach back here to grab them at module-init time.
router = APIRouter(tags=["chat"])
executor = ThreadPoolExecutor(max_workers=8)

# --- Layer 1: infrastructure (no internal deps) ------------------------
# --- Layer 5: routes (decorator side-effects register HTTP handlers) ---
# Imported for side-effects only; nothing in here is part of the public API.
from . import routes  # noqa: E402,F401

# --- Layer 3: budgeting (no internal deps) -----------------------------
from .budget import (  # noqa: E402,F401
    TOOL_RESULT_BUDGET_BYTES,
    _budget_tool_result,
    make_budget_tool_result,
)

# --- Layer 2: compaction (depends on infra + executor) -----------------
from .compaction import (  # noqa: E402,F401
    COMPACT_TRIGGER_CHARS,
    MAX_CONTEXT_CHARS,
    _compact_messages_simple,
    compact_messages_with_claude,
    estimate_message_size,
    microcompact_messages,
)
from .infra import (  # noqa: E402,F401
    _BEDROCK_CLIENT_LOCK,
    _BEDROCK_CLIENTS,
    _estimate_cost,
    _get_bedrock_client,
    _get_chat_model_meta,
    _get_chat_models,
    _get_default_model,
    _reset_bedrock_client_cache,
)

# --- Layer 4: tool catalogue (no internal deps) ------------------------
from .tool_pool import (  # noqa: E402,F401
    _BUILTIN_CONCURRENT_SAFE,
    _is_tool_concurrent_safe,
    assemble_tool_pool,
)
