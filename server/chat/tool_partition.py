"""Core/deferred tool partition for progressive disclosure.

Every turn used to ship all ~70-99 tool schemas (~15-25K tokens of JSON) to
the model. Progressive disclosure advertises a curated CORE set plus whatever
this session has ACTIVATED (via tool_search or history replay); everything
else appears only as a one-line entry in the deferred index inside the system
prompt, discoverable and loadable on demand.

The partition applies AFTER the existing mode filters (plan-mode blocks,
strict-RAG suppression): an activated tool can never bypass a mode block,
because activation intersects with the post-filter catalog.
"""

import logging

log = logging.getLogger("whisper-studio")

# Curated always-advertised set: the primitives nearly every turn touches,
# the discovery tools themselves (tool_search must never be deferrable — it
# is the way back), and the orchestration tools ultracode depends on seeing.
CORE_TOOLS: frozenset[str] = frozenset(
    {
        # workspace primitives
        "ws_read_file",
        "ws_write_file",
        "ws_edit_file",
        "ws_create_file",
        "ws_list_directory",
        "ws_run_command",
        "ws_grep",
        "ws_glob",
        "workspace_semantic_search",
        # discovery — never deferred
        "tool_search",
        "skill_list",
        "skill_invoke",
        # interaction
        "ask_user_question",
        "notify_user",
        "read_cached_result",
        # todo tracker (in-conversation plans)
        "task_create",
        "task_update",
        "task_list",
        "task_get",
        "task_stop",
        # background tasks (unified registry)
        "task_status",
        "task_output",
        # orchestration (ultracode's directive references these by name)
        "spawn_agent",
        "team_create",
        # workflow runtime — only present in the catalog in ultracode mode, but
        # core so they're never deferred behind tool_search when they ARE there.
        "workflow_run",
        "workflow_status",
        "workflow_save",
        "workflow_list",
        # CI watch + autofix — likewise ultracode-only in the catalog, core when
        # present so they aren't deferred behind tool_search.
        "ci_watch",
        "ci_status",
        "ci_autofix",
        # misc core
        "sleep",
        "git_status",
        "git_diff",
    }
)


def core_names() -> frozenset[str]:
    """The effective core set: curated constant plus config extras."""
    extra: set[str] = set()
    defer: set[str] = set()
    try:
        from server.infrastructure.config import load_config

        cfg = load_config()
        extra = {str(n) for n in (cfg.get("progressive_tools_core_extra") or [])}
        defer = {str(n) for n in (cfg.get("progressive_tools_defer_extra") or [])}
    except Exception:
        pass
    # tool_search is the way back to everything else; it can never defer.
    defer.discard("tool_search")
    return (CORE_TOOLS | extra) - defer


def partition_pool(catalog: list[dict], activated: list[str]) -> tuple[list[dict], list[dict], int]:
    """Split a post-mode-filter catalog into (advertised, deferred, core_count).

    Advertised ordering is cache-critical: core tools keep the catalog's
    existing sorted order (stable prefix), activated tools are APPENDED in
    activation order and never re-sorted, so each new activation invalidates
    only the appended tail of the tools cache block, not the core prefix.
    ``core_count`` is the length of that stable prefix — the caching layer
    places a breakpoint on tools[core_count-1] so the prefix keeps hitting
    across activation events.
    """
    core = core_names()
    by_name = {t["name"]: t for t in catalog}
    advertised: list[dict] = [t for t in catalog if t["name"] in core]
    core_count = len(advertised)
    seen = {t["name"] for t in advertised}
    for name in activated:
        tool = by_name.get(name)
        if tool is not None and name not in seen:
            advertised.append(tool)
            seen.add(name)
    deferred = [t for t in catalog if t["name"] not in seen]
    return advertised, deferred, core_count
