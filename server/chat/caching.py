"""Prompt-cache parameters and checkpoint helpers for Bedrock Claude models.

engine/anthropic.py places the ``cache_control`` checkpoints through the
helpers here: the tool block (``cached_tools_and_system``), the static system
block, and a moving checkpoint on the conversation
(``annotate_messages_cache``).

Every Claude model the app ships supports the 1-hour TTL. For user-paced chat
(turns often more than 5 minutes apart) the 1-hour TTL is the main lever that
keeps caching a net win instead of paying repeated write premiums.
"""

from __future__ import annotations

import copy

# The cache_control ttl the app sends on every checkpoint.
CACHE_TTL = "1h"


def resolve_system_prompt(
    model_id: str,
    *,
    caching_on: bool,
    **sp_kwargs,
) -> tuple[str, str | None, str | None, str | None]:
    """Build the system prompt for one chat request.

    Returns ``(system_prompt, system_static, system_dynamic, cache_ttl)``:
      - caching on  -> system_static/dynamic are the split blocks and cache_ttl
        is the model's TTL; system_prompt is their join (for code that reads the
        string and for the uncached final round).
      - caching off -> system_static/dynamic/cache_ttl are None; system_prompt is
        the joined string.
    ``sp_kwargs`` are the build_system_prompt args (ws_path, session_id, etc.).
    """
    from server.prompts import build_system_prompt, build_system_prompt_split

    if caching_on:
        static, dynamic = build_system_prompt_split(**sp_kwargs)
        return static + dynamic, static, dynamic, CACHE_TTL
    return build_system_prompt(**sp_kwargs), None, None, None


def cached_tools_and_system(
    all_tools: list[dict],
    system_static: str,
    system_dynamic: str,
    ttl: str,
    core_count: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """Apply cache_control checkpoints for the tools-present case.

    Cache order is tools -> system -> messages, so we checkpoint the LAST tool
    and the static system block. Returns ``(tools_field, system_field)``:
      - tools_field: a deep copy of ``all_tools`` with cache_control on the last
        tool (never mutate the shared, reused pool in place).
      - system_field: ``[{static, cache_control}, {dynamic}]``: the static
        prefix cached, the volatile tail uncached. The dynamic block is omitted
        when empty so we never send an empty content block.

    ``core_count`` (progressive disclosure): when session-activated tools are
    appended after the stable core prefix, an EXTRA checkpoint on
    tools[core_count-1] keeps the big core prefix hitting across activation
    events: each activation then re-writes only (activated schemas + static
    system), not the whole tools block. Breakpoint budget: core boundary +
    tools-last + static system + the moving message checkpoint = exactly the
    4 Anthropic allows.
    """
    tools_field = copy.deepcopy(all_tools)
    tools_field[-1]["cache_control"] = {"type": "ephemeral", "ttl": ttl}
    if core_count is not None and 0 < core_count < len(tools_field):
        tools_field[core_count - 1]["cache_control"] = {"type": "ephemeral", "ttl": ttl}

    system_field: list[dict] = [
        {
            "type": "text",
            "text": system_static,
            "cache_control": {"type": "ephemeral", "ttl": ttl},
        }
    ]
    if system_dynamic:
        system_field.append({"type": "text", "text": system_dynamic})
    return tools_field, system_field


def annotate_messages_cache(messages: list[dict], ttl: str) -> list[dict]:
    """Third breakpoint: a MOVING checkpoint on the conversation history.

    Without it the entire messages array (which dominates long agentic turns)
    is re-billed as uncached input on every round of every turn. Bedrock
    resolves cache hits by longest-prefix lookback over recent breakpoint
    positions, so one checkpoint on the LAST message yields round-over-round
    incremental reads within a turn and turn-over-turn reads across turns.

    Returns a REQUEST-ONLY copy: the list is shallow-copied and only the last
    message is deep-copied before ``cache_control`` is set on its final content
    block. The shared ``messages`` list is NEVER mutated: those dicts are
    persisted to chat history and replayed by the OpenAI and local paths,
    where a stray ``cache_control`` key would fail the request or leak into
    storage. String content is converted to a text-block list as needed.

    Breakpoint budget: core boundary (1) + tools-last (1) + static system (1)
    + this (1) = the 4 Anthropic allows (see cached_tools_and_system).
    """
    if not messages:
        return messages
    out = list(messages)
    last = copy.deepcopy(out[-1])
    content = last.get("content")
    if isinstance(content, str):
        if not content:
            return messages
        content = [{"type": "text", "text": content}]
        last["content"] = content
    if not isinstance(content, list) or not content:
        return messages
    final_block = content[-1]
    if not isinstance(final_block, dict):
        return messages
    final_block["cache_control"] = {"type": "ephemeral", "ttl": ttl}
    out[-1] = last
    return out
