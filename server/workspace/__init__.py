"""Workspace package: filesystem ops, tool schemas, executors, and HTTP routes.

Modules:

    paths       file I/O primitives, path validation, the mutable backup dict
    state       workspace config and recent list persistence, connect/disconnect
    filesystem  directory listing and filename search
    tools       tool schemas surfaced to the model
    commands    shell-execution helpers for ws_run_command and approved commands
    executors   @register_executor tool implementations and worktree state
    routes      the /api/workspace FastAPI handlers

The router instance lives here so every route module registers on it with
``from .. import router``. Importing ``routes`` and ``executors`` below runs
their decorators. The names re-exported here are the ones other packages
read off ``server.workspace``; everything else is imported from its module.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/workspace", tags=["workspace"])

from . import routes  # noqa: E402,F401
from .commands import _validate_command  # noqa: E402,F401
from .executors import execute_ws_open_folder  # noqa: E402,F401
from .paths import (  # noqa: E402,F401
    _atomic_write_bytes,
    _atomic_write_text,
    _normalize_lf,
    _ws_path_allowed,
    _ws_validate_path,
    own_scratch_dir,
    ws_path_changeable,
)
from .state import (  # noqa: E402,F401
    connect_workspace,
    get_workspace_path,
    is_plan_mode,
)
from .tools import (  # noqa: E402,F401
    get_global_workspace_tools,
    get_workspace_tools,
    get_workspace_write_tools,
    get_worktree_tools,
)
