"""Three-tier tool catalogue assembly.

Tier 1 — catalogue: union of every tool source (skills, workspace, git,
LSP, notebook, agents, cron, memory, MCP, etc.).

Tier 2 — mode filter: strip tools blocked by the current permissions
mode (e.g. plan mode forbids file mutators).

Tier 3 — sort & merge: built-ins first (sorted by name), MCP tools next
(also sorted), deduplicated. Bedrock rejects requests with duplicate
tool names so dedup is mandatory.

``_is_tool_concurrent_safe`` lives here because it's read directly by
the chat endpoint when deciding whether to run a tool batch in parallel —
keeping it next to the catalogue keeps the safety classification close
to the catalogue that produced the tool.
"""

import os

from server.agent_tools import AGENT_TOOLS
from server.ask_user import ALL_TOOLS as ASK_USER_TOOLS
from server.cron_scheduler import CRON_TOOLS
from server.executors import is_concurrent_safe as _executor_concurrent_safe
from server.executors.result_cache import RESULT_CACHE_TOOLS
from server.git.tools import get_git_tools
from server.lsp import LSP_TOOLS
from server.mcp import mcp_manager
from server.notebook import NOTEBOOK_TOOLS
from server.plans.tools import PLAN_TOOLS
from server.skills import TOOLS
from server.tasks_tracker import TASK_TOOLS
from server.tool_executor import _PLAN_MODE_BLOCKED
from server.workspace import (
    get_global_workspace_tools,
    get_workspace_path,
    get_workspace_tools,
    get_worktree_tools,
)

# Workspace file/search tools withheld on strict-RAG turns. When the answer is
# already grounded in injected index passages, offering these only tempts the
# model to re-crawl the workspace instead of answering from the passages.
_WS_SEARCH_TOOLS = {
    "workspace_semantic_search",
    "workspace_graph_query",
    "ws_read_file",
    "ws_grep",
    "ws_glob",
    "ws_list_directory",
}


# Tools known to be concurrent-safe that don't go through executor registry
# (handled inline in chat.py or by specialized handlers).
_BUILTIN_CONCURRENT_SAFE = {
    "lsp_diagnostics",
    "lsp_hover",
    "lsp_references",
    "task_list",
    "task_get",
    "cron_list",
    "notebook_read",
    "skill_list",
    "tool_search",
    "list_mcp_resources",
    "read_mcp_resource",
    "config_get",
    "list_agents",
    "memory_read",
    "memory_list",
}


def _is_tool_concurrent_safe(tool_name: str) -> bool:
    """Check if a tool call is safe for parallel execution.

    Uses executor metadata when available, falls back to built-in set.
    MCP tools are treated as concurrent-safe (they're external read calls).
    """
    if _executor_concurrent_safe(tool_name):
        return True
    if tool_name in _BUILTIN_CONCURRENT_SAFE:
        return True
    if mcp_manager.is_mcp_tool(tool_name):
        return True
    return False


def assemble_tool_pool(
    *,
    plan_mode: bool = False,
    ws_connected: bool = False,
    mcp_enabled_names: set[str] | None = None,
    suppress_workspace_search: bool = False,
    session_id: str = "",
    progressive: bool = False,
) -> list[dict]:
    """Advertised tool pool for one model request.

    Default (``progressive=False``) is the byte-identical legacy full pool —
    cron and the agent runtime keep their existing behavior. The interactive
    chat loop passes ``progressive=True``: when the ``progressive_tools``
    flag is on, the pool is partitioned to core + this session's activated
    tools, with everything else discoverable via tool_search.
    """
    catalog = assemble_full_catalog(
        plan_mode=plan_mode,
        ws_connected=ws_connected,
        mcp_enabled_names=mcp_enabled_names,
        suppress_workspace_search=suppress_workspace_search,
    )
    if not progressive or not session_id:
        return catalog
    from server.infrastructure.feature_flags import is_enabled as _ff_enabled

    if not _ff_enabled("progressive_tools"):
        return catalog
    from server.chat.tool_activation import get_ordered
    from server.chat.tool_partition import partition_pool

    advertised, _deferred, _core_count = partition_pool(catalog, get_ordered(session_id))
    return advertised


def assemble_partitioned_pool(
    *,
    plan_mode: bool = False,
    ws_connected: bool = False,
    mcp_enabled_names: set[str] | None = None,
    suppress_workspace_search: bool = False,
    session_id: str = "",
    ultracode: bool = False,
) -> tuple[list[dict], list[dict], int]:
    """(advertised, deferred, core_count) for callers that also need the
    deferred list (the system-prompt index) and the cache-breakpoint count.
    Flag off (or no session): full catalog, nothing deferred."""
    catalog = assemble_full_catalog(
        plan_mode=plan_mode,
        ws_connected=ws_connected,
        mcp_enabled_names=mcp_enabled_names,
        suppress_workspace_search=suppress_workspace_search,
        ultracode=ultracode,
    )
    from server.infrastructure.feature_flags import is_enabled as _ff_enabled

    if not session_id or not _ff_enabled("progressive_tools"):
        return catalog, [], len(catalog)
    from server.chat.tool_activation import get_ordered
    from server.chat.tool_partition import partition_pool

    return partition_pool(catalog, get_ordered(session_id))


def assemble_full_catalog(
    *,
    plan_mode: bool = False,
    ws_connected: bool = False,
    mcp_enabled_names: set[str] | None = None,
    suppress_workspace_search: bool = False,
    ultracode: bool = False,
) -> list[dict]:
    """Three-tier tool assembly: catalog → mode filter → sorted pool.

    Tier 1 (catalog): All registered tools from all sources.
    Tier 2 (mode filter): Remove tools incompatible with current mode.
    Tier 3 (sort & merge): Built-ins sorted, MCP sorted, concatenated.

    `mcp_enabled_names` controls which MCP servers' tools are exposed:
    - None (default): each server's persisted `enabled` flag is honoured.
    - Explicit set: only those servers' tools are advertised — used for
      per-request overrides from the chat toolbar's MCP checklist.

    This is the PRE-PARTITION catalog: also the single source of truth for
    tool_search, which must see every tool regardless of deferral.
    """
    # Tier 1: full catalog
    builtin_tools = list(TOOLS)  # skill-based tools
    builtin_tools += get_global_workspace_tools()
    # git_clone is the one git tool that works WITHOUT a connected workspace
    # — by design, it creates one. Always include it so the assistant can
    # respond to "clone this repo" from a fresh state.
    from server.git.tools import GIT_WRITE_TOOLS

    for t in GIT_WRITE_TOOLS:
        if t["name"] == "git_clone":
            builtin_tools.append(t)
            break
    if ws_connected:
        builtin_tools += get_workspace_tools()
        builtin_tools += get_worktree_tools()
        # Add git tools when workspace is a git repo
        ws = get_workspace_path()
        if ws and os.path.exists(os.path.join(ws, ".git")):
            # `git_clone` is already in the catalog above; the rest of
            # get_git_tools() needs the repo. Dedup happens in Tier 3.
            builtin_tools += get_git_tools()
            # GitHub hybrid tools (verb + raw API planes). Deferred via the
            # partition below (not in CORE_TOOLS), so zero context cost until
            # tool_search activates them.
            from server.git.gh_tools import get_github_tools

            builtin_tools += get_github_tools()
    builtin_tools += RESULT_CACHE_TOOLS
    builtin_tools += TASK_TOOLS
    from server.tasks.tools import BACKGROUND_TASK_TOOLS

    builtin_tools += BACKGROUND_TASK_TOOLS
    builtin_tools += PLAN_TOOLS
    builtin_tools += CRON_TOOLS
    builtin_tools += LSP_TOOLS
    builtin_tools += ASK_USER_TOOLS
    builtin_tools += NOTEBOOK_TOOLS
    builtin_tools += AGENT_TOOLS

    # Workflow runtime + CI tools surface ONLY in ultracode mode (the real
    # ultracode): CI autofix hands its fix off to the workflow runtime.
    if ultracode:
        from server.ci.tools import CI_TOOLS
        from server.workflows.tools import WORKFLOW_TOOLS

        builtin_tools += WORKFLOW_TOOLS
        builtin_tools += CI_TOOLS

    # Memory tools (feature-flag gated)
    from server.infrastructure.feature_flags import is_enabled as _ff_enabled

    if _ff_enabled("auto_memory"):
        from server.memory.tools import MEMORY_TOOLS

        builtin_tools += MEMORY_TOOLS

    # Live preview tools — flag AND capability probe must both pass, so the
    # tools are genuinely absent from the catalog (not just disabled) until
    # the user has both opted in AND completed the Playwright/Chromium
    # install. Checked live (cheap — no subprocess), not cached at
    # flag-toggle time, so a corrupted/deleted install hides the tools again
    # automatically.
    if _ff_enabled("preview_tools"):
        from server.preview.capability import preview_capability_ok

        if preview_capability_ok():
            from server.preview.tools import PREVIEW_TOOLS

            builtin_tools += PREVIEW_TOOLS

    mcp_tools = list(mcp_manager.get_bedrock_tools(enabled_names=mcp_enabled_names))

    # Tier 2: mode filtering
    if plan_mode:
        builtin_tools = [t for t in builtin_tools if t["name"] not in _PLAN_MODE_BLOCKED]
    # Strict-RAG: when this turn is already grounded in injected passages, drop
    # the workspace file/search tools so the model answers from them.
    if suppress_workspace_search:
        builtin_tools = [t for t in builtin_tools if t["name"] not in _WS_SEARCH_TOOLS]

    # Tier 3: sort each group, concatenate, deduplicate
    builtin_tools.sort(key=lambda t: t["name"])
    mcp_tools.sort(key=lambda t: t["name"])
    combined = builtin_tools + mcp_tools

    # Deduplicate by name — MCP servers may contribute tools that clash with
    # built-ins or with each other. Bedrock rejects non-unique tool names.
    seen = set()
    deduped = []
    for t in combined:
        if t["name"] not in seen:
            seen.add(t["name"])
            deduped.append(t)
    return deduped
