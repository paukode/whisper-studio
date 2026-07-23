"""Guardrail: the workspace and chat packages must keep re-exporting the
public (and a few private-but-imported) names their callers rely on.

When server/workspace.py and server/chat.py were split into packages, their
behaviour-preserving contract was "every symbol external code imports off
`server.workspace` / `server.chat` still resolves." A future edit that moves
or renames a symbol but forgets to update the package __init__ re-export
would break importers at runtime — this test catches that at CI time.

The lists below mirror the actual import sites across the codebase (chat,
skills, lsp, git, memory, search, agents, approval/bootstrap, main, tests).
"""

import importlib

WORKSPACE_EXPORTS = [
    # public API
    "router",
    "get_workspace_path",
    "get_workspace_mode",
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
    "_get_bedrock_client",
    "_reset_bedrock_client_cache",
    "_get_chat_models",
    "_get_chat_model_meta",
    "_get_default_model",
    "_estimate_cost",
    "estimate_message_size",
    "COMPACT_TRIGGER_CHARS",
    "MAX_CONTEXT_CHARS",
    "microcompact_messages",
    "compact_messages_with_claude",
    "_compact_messages_simple",
    "TOOL_RESULT_BUDGET_BYTES",
    "_budget_tool_result",
    "make_budget_tool_result",
    "assemble_tool_pool",
    "_is_tool_concurrent_safe",
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
