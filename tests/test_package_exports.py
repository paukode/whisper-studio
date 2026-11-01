"""Guardrail: the workspace and chat packages keep the names their callers
import off the package itself.

server.workspace re-exports its public and a few private names. server.chat
binds only ``router`` and ``executor``; everything else is imported from its
own submodule. A move or rename that forgets the package would break
importers at runtime, and this test catches it first.
"""

import importlib

WORKSPACE_EXPORTS = [
    # public API
    "router",
    "get_workspace_path",
    "get_workspace_tools",
    "get_global_workspace_tools",
    "get_worktree_tools",
    "execute_ws_open_folder",
    "is_plan_mode",
    "connect_workspace",
    "load_workspace_config",
    "save_workspace_config",
    "load_recent_workspaces",
    "save_recent_workspace",
    # private symbols imported by other modules / tests
    "_ws_validate_path",
    "_atomic_write_text",
    "_check_writable",
    "_normalize_lf",
    "_apply_stdin_redirect",
    "_needs_stdin_redirect",
    "_truncate_shell_output",
    "WORKSPACE_BACKUPS",
    "DATA_DIR",
]

CHAT_EXPORTS = [
    "router",
    "executor",
]


def test_workspace_package_reexports_intact():
    mod = importlib.import_module("server.workspace")
    missing = [name for name in WORKSPACE_EXPORTS if not hasattr(mod, name)]
    assert not missing, f"server.workspace lost re-exports: {missing}"


def test_chat_package_reexports_intact():
    mod = importlib.import_module("server.chat")
    missing = [name for name in CHAT_EXPORTS if not hasattr(mod, name)]
    assert not missing, f"server.chat lost re-exports: {missing}"


def test_workspace_tool_executors_registered():
    """Importing the package must fire every @register_executor so the tool
    dispatcher can find the workspace tools."""
    import server.workspace  # noqa: F401  (import for the registration side-effect)
    from server.executors import EXECUTORS

    expected = {
        "ws_read_file",
        "ws_write_file",
        "ws_edit_file",
        "ws_create_file",
        "ws_delete_file",
        "ws_list_directory",
        "ws_run_command",
        "ws_create_worktree",
        "ws_diff_worktree",
        "ws_merge_worktree",
    }
    missing = expected - set(EXECUTORS)
    assert not missing, f"workspace executors not registered on import: {missing}"
