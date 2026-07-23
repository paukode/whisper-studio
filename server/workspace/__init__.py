"""Workspace package: filesystem ops, tool schemas, executors, and HTTP routes.

This package was split out of the former monolithic `server/workspace.py`
(1932 lines) into a layered structure:

    paths       — file I/O primitives, path validation, mutable backup dict
    state       — workspace config + recent list persistence, connect/disconnect
    filesystem  — directory listing and filename search
    tools       — tool schemas surfaced to the LLM
    commands    — shell-execution helpers (shared by executors and routes)
    executors   — @register_executor tool implementations + worktree state
    routes      — @router.* FastAPI HTTP handlers

The order of imports below matters: every name that external code reads off
``server.workspace`` must be bound to this package's namespace BEFORE the
``executors`` and ``routes`` submodules run (their decorators are import-time
side effects). The router instance lives here so all submodules can reach it
via ``from . import router`` once this file has executed up to that line.
"""

from fastapi import APIRouter

# Public router. Must exist before `from . import routes` runs so routes.py
# can register its handlers on it.
router = APIRouter(prefix="/api/workspace", tags=["workspace"])

# --- Layer 1: pure utilities (no internal deps) ------------------------
# --- Layer 7: routes (decorator side-effects register HTTP handlers) ---
# Imported for side-effects only; nothing in here is part of the public API.
from . import routes  # noqa: E402,F401

# --- Layer 4: shell-execution helpers ----------------------------------
from .commands import (  # noqa: E402,F401
    _apply_stdin_redirect,
    _detect_image_output,
    _interpret_exit_code,
    _is_silent_command,
    _needs_stdin_redirect,
    _truncate_shell_output,
    _validate_command,
)

# --- Layer 6: executors (decorator side-effects register tool handlers)
# Importing the module is what runs @register_executor — we also pull
# the executor-bridge `execute_ws_open_folder` into the package namespace
# because chat.py and tool_router.py call it directly.
from .executors import (  # noqa: E402,F401
    _WORKTREES,
    _is_read_only_command,
    _normalize_quotes,
    execute_ws_open_folder,
)

# --- Layer 3: filesystem helpers ---------------------------------------
from .filesystem import (  # noqa: E402,F401
    _ws_list_dir,
    _ws_search_files,
)
from .paths import (  # noqa: E402,F401
    _BLOCKED_PATH_PREFIXES,
    _WS_BINARY_EXTS,
    _WS_IGNORED_DIRS,
    _WS_IMAGE_EXTS,
    DATA_DIR,
    RECENT_WORKSPACES_PATH,
    WORKSPACE_BACKUPS,
    WORKSPACE_CONFIG_PATH,
    _atomic_write_text,
    _normalize_lf,
    _resolve_path,
    _strip_trailing_ws,
    _ws_validate_path,
)

# --- Layer 2: workspace state (depends on paths) -----------------------
from .state import (  # noqa: E402,F401
    _check_writable,
    _workspace_prompt_payload,
    connect_workspace,
    get_workspace_mode,
    get_workspace_path,
    is_plan_mode,
    load_recent_workspaces,
    load_workspace_config,
    save_recent_workspace,
    save_workspace_config,
)

# --- Layer 5: tool schemas ---------------------------------------------
from .tools import (  # noqa: E402,F401
    get_global_workspace_tools,
    get_workspace_tools,
    get_worktree_tools,
)
