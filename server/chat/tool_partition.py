"""Core/deferred tool partition for progressive disclosure.

Advertising all ~70-99 tool schemas costs ~15-25K tokens of JSON per turn.
Progressive disclosure advertises a curated CORE set plus whatever
this session has ACTIVATED (via tool_search or history replay); everything
else appears only as a one-line entry in the deferred index inside the system
prompt, discoverable and loadable on demand.

The partition applies to the full stable catalog. Per-turn mode filters
(plan-mode blocks, strict-RAG suppression) no longer strip the catalog —
they are enforced at execution time — so activation intersects with a
catalog that is byte-stable across those flips.
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
        # visuals — "draw me a diagram" must work on the first turn
        "create_visual",
        "create_chart",
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

# Core in a chat turn only. The memory tools are in the catalog only while
# auto_memory is on, so core exactly then: the post-turn learning review
# replays the chat turn's tools array byte for byte
# (server/memory/review_fork.py) and cannot load a deferred tool, so the
# tools it saves with must be advertised. Voice turns keep them as well (a
# person is there to ask for a memory). Agents, scheduled tasks and other
# unattended runs have no review fork; there the tools stay one tool_search
# away, so they do not carry their schemas on every request nor write or
# delete memories unasked.
CHAT_CORE_TOOLS: frozenset[str] = frozenset(
    {"memory_read", "memory_write", "memory_list", "memory_delete"}
)


def core_names(*, chat: bool = True) -> frozenset[str]:
    """The effective core set: curated constant, the chat-only memory tools
    and skill_manage while skill_self_improvement is on (``chat``), plus
    config extras.

    skill_manage is in the catalog either way (its executor refuses while the
    flag is off); in a chat turn it is core while the flag lets the chat and
    the learning review call it, for the same replay reason as the memory
    tools.

    ``progressive_tools_core_extra`` / ``progressive_tools_defer_extra`` are
    hand-edit-only operator knobs (no UI, not in the example config): lists of
    tool names to force into or out of the always-advertised core set."""
    extra: set[str] = set()
    defer: set[str] = set()
    try:
        from server.infrastructure.config import load_config
        from server.infrastructure.feature_flags import is_enabled

        cfg = load_config()
        extra = {str(n) for n in (cfg.get("progressive_tools_core_extra") or [])}
        defer = {str(n) for n in (cfg.get("progressive_tools_defer_extra") or [])}
        if chat and is_enabled("skill_self_improvement"):
            extra.add("skill_manage")
    except Exception:
        pass
    if chat:
        extra |= CHAT_CORE_TOOLS
    # tool_search is the way back to everything else; it can never defer.
    defer.discard("tool_search")
    return (CORE_TOOLS | extra) - defer


def partition_pool(
    catalog: list[dict], activated: list[str], *, chat: bool = True
) -> tuple[list[dict], list[dict], int]:
    """Split a post-mode-filter catalog into (advertised, deferred, core_count).

    Advertised ordering is cache-critical: core tools keep the catalog's
    existing sorted order (stable prefix), activated tools are APPENDED in
    activation order and never re-sorted, so each new activation invalidates
    only the appended tail of the tools cache block, not the core prefix.
    ``core_count`` is the length of that stable prefix — the caching layer
    places a breakpoint on tools[core_count-1] so the prefix keeps hitting
    across activation events.
    """
    core = core_names(chat=chat)
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
