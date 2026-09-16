"""Tool catalogue assembly.

Tier 1 — catalogue: union of every tool source (skills, workspace, git,
LSP, notebook, agents, cron, memory, MCP, etc.). The catalogue is
deliberately STABLE across per-turn state flips (plan mode, strict-RAG
grounding, workspace connect/disconnect): register always, refuse at
execution. A tools array that changes between turns invalidates the
prompt-prefix cache, so per-turn filters live at execution time, not here.

Tier 3 — sort & merge: built-ins first (sorted by name), MCP tools next
(also sorted), deduplicated. Bedrock rejects requests with duplicate
tool names so dedup is mandatory. (Tier 2, the per-turn mode filter, was
retired — see the comment above Tier 3 in assemble_full_catalog.)

``_is_tool_concurrent_safe`` lives here because it's read directly by
the chat endpoint when deciding whether to run a tool batch in parallel —
keeping it next to the catalogue keeps the safety classification close
to the catalogue that produced the tool.
"""

from server.agent_tools import AGENT_TOOLS
from server.ask_user import ALL_TOOLS as ASK_USER_TOOLS
from server.cron_scheduler import CRON_TOOLS
from server.documents.tools import DOCUMENT_TOOLS
from server.executors import is_concurrent_safe as _executor_concurrent_safe
from server.executors.result_cache import RESULT_CACHE_TOOLS
from server.executors.tools import CORE_EXECUTOR_TOOLS
from server.git.tools import get_git_tools
from server.lsp import LSP_TOOLS
from server.mcp import mcp_manager
from server.notebook import NOTEBOOK_TOOLS
from server.plans.tools import PLAN_TOOLS
from server.prompt_tools import PROMPT_TOOLS
from server.skills import TOOLS
from server.tasks_tracker import TASK_TOOLS
from server.visuals import VISUAL_TOOLS
from server.workspace import (
    get_global_workspace_tools,
    get_workspace_tools,
    get_workspace_write_tools,
    get_worktree_tools,
)

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
    "session_search",
    "read_artifact",
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
    suppress_workspace_search: bool = False,
) -> list[dict]:
    """Full advertised tool pool for one model request — cron and the agent
    runtime use it. The interactive chat loop uses
    ``assemble_partitioned_pool`` instead (progressive core + activated
    tools, everything else discoverable via tool_search)."""
    return assemble_full_catalog(
        plan_mode=plan_mode,
        ws_connected=ws_connected,
        suppress_workspace_search=suppress_workspace_search,
    )


def assemble_partitioned_pool(
    *,
    plan_mode: bool = False,
    ws_connected: bool = False,
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
    suppress_workspace_search: bool = False,
    ultracode: bool = False,
) -> list[dict]:
    """Three-tier tool assembly: catalog → mode filter → sorted pool.

    Tier 1 (catalog): All registered tools from all sources.
    Tier 2 (mode filter): Remove tools incompatible with current mode.
    Tier 3 (sort & merge): Built-ins sorted, MCP sorted, concatenated.

    This is the PRE-PARTITION catalog: also the single source of truth for
    tool_search, which must see every tool regardless of deferral.
    """
    # Tier 1: full catalog
    builtin_tools = list(TOOLS)  # skill-based tools
    builtin_tools += CORE_EXECUTOR_TOOLS
    builtin_tools += PROMPT_TOOLS
    builtin_tools += get_global_workspace_tools()
    # git_clone is the one git tool that works WITHOUT a connected workspace
    # — by design, it creates one. Always include it so the assistant can
    # respond to "clone this repo" from a fresh state.
    from server.git.tools import GIT_WRITE_TOOLS

    for t in GIT_WRITE_TOOLS:
        if t["name"] == "git_clone":
            builtin_tools.append(t)
            break
    # Workspace, worktree, git, and GitHub tools are in the catalog
    # UNCONDITIONALLY — the catalog is deliberately independent of whether a
    # workspace is connected right now. Their executors gate at execution
    # ("No workspace connected", "not a git repo", or a folder picker), so a
    # mid-session connect/disconnect no longer rewrites the tools array and
    # invalidates the prompt-prefix cache (register always, refuse at
    # execution). The write-tool variants come last so the workspace schemas
    # win the Tier-3 dedup on shared names.
    builtin_tools += get_workspace_tools()
    builtin_tools += get_worktree_tools()
    builtin_tools += get_git_tools()
    from server.git.gh_tools import get_github_tools

    builtin_tools += get_github_tools()
    builtin_tools += get_workspace_write_tools()
    # Document tools (create_docx/pptx/xlsx/pdf, office_script,
    # inspect_document) follow the same contract as the write tools above:
    # advertised with or without a connected workspace, since each executor
    # answers "no workspace" with its own folder-picker prompt rather than
    # an error.
    builtin_tools += DOCUMENT_TOOLS
    builtin_tools += RESULT_CACHE_TOOLS
    builtin_tools += TASK_TOOLS
    from server.tasks.tools import BACKGROUND_TASK_TOOLS

    builtin_tools += BACKGROUND_TASK_TOOLS
    builtin_tools += PLAN_TOOLS
    builtin_tools += CRON_TOOLS
    builtin_tools += LSP_TOOLS
    builtin_tools += ASK_USER_TOOLS
    # read_artifact / edit_artifact: change a delivered app in place instead of
    # regenerating it. Deferred until an artifact exists (activated by
    # create_artifact and by history replay), discoverable via tool_search.
    from server.artifacts import ARTIFACT_TOOLS

    builtin_tools += ARTIFACT_TOOLS
    builtin_tools += VISUAL_TOOLS
    builtin_tools += NOTEBOOK_TOOLS
    builtin_tools += AGENT_TOOLS

    # User plugins: tools contributed by ENABLED plugins whose executors are
    # live in the EXECUTORS registry. Gated on the persisted enabled set inside
    # plugin_tools(), so toggling a plugin off hides its tools on the next turn
    # without a restart. Appended after the built-ins so core schemas win the
    # Tier-3 dedup on any name clash.
    from server.plugins import plugin_tools

    builtin_tools += plugin_tools()

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

    mcp_tools = list(mcp_manager.get_bedrock_tools())

    # Tier 2 (retired): per-turn mode filters used to strip tools here, which
    # rewrote the tools array on every plan-mode toggle and every strict-RAG
    # round and invalidated the prompt-prefix cache. The catalog is now stable
    # across those flips; enforcement moved to where it always also lived:
    #   - plan mode: tool_executor blocks _PLAN_MODE_BLOCKED at execution and
    #     tells the model why, so the schemas can stay advertised.
    #   - strict-RAG: the grounding block itself instructs the model to answer
    #     from the injected passages instead of re-crawling (see
    #     server/index/pipeline.py); withholding the schemas was belt and
    #     suspenders that cost a cache miss per grounded turn.
    # The plan_mode / suppress_workspace_search parameters are kept so call
    # sites don't churn, and because execution-level consumers still key off
    # the same per-turn state.

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
