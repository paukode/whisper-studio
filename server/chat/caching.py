"""Prompt-cache parameters per Bedrock Claude model.

The caching itself (placing ``cache_control`` checkpoints on the tool block and
the static system block) lives in ``server/chat/routes.py``. This module only
decides the cache TTL for a given model, since Bedrock supports a 1-hour TTL on
some Claude models and only the 5-minute default on others.

Per the AWS Bedrock prompt-caching docs (verified):
  - 5-minute TTL only:  Claude Opus 4.6, Claude Sonnet 4.6
  - 5-minute + 1-hour:  Claude Opus 4.5/4.7/4.8, Claude Haiku 4.5, Fable 5

For user-paced chat (turns often more than 5 minutes apart) the 1-hour TTL is
the main lever that keeps caching a net win instead of paying repeated write
premiums, so we prefer it wherever it is supported.

Keyed off the resolved Bedrock model id (e.g. ``global.anthropic.claude-opus-4-8``).
"""

from __future__ import annotations

import copy

# model_id substrings that get ONLY the 5-minute TTL on Bedrock. Everything else
# (Opus 4.7/4.8, Haiku 4.5, Fable 5, Opus 4.5) supports the 1-hour TTL.
_TTL_5M_ONLY = ("claude-opus-4-6", "claude-sonnet-4-6")


def cache_ttl_for(model_id: str) -> str:
    """Return the cache_control ttl ("1h" or "5m") to use for this model id."""
    mid = (model_id or "").lower()
    for sub in _TTL_5M_ONLY:
        if sub in mid:
            return "5m"
    return "1h"


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
        the legacy joined string.
    ``sp_kwargs`` are the build_system_prompt args (ws_path, session_id, etc.).
    """
    from server.prompts import build_system_prompt, build_system_prompt_split

    if caching_on:
        static, dynamic = build_system_prompt_split(**sp_kwargs)
        return static + dynamic, static, dynamic, cache_ttl_for(model_id)
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
      - system_field: ``[{static, cache_control}, {dynamic}]`` — the static
        prefix cached, the volatile tail uncached. The dynamic block is omitted
        when empty so we never send an empty content block.

    ``core_count`` (progressive disclosure): when session-activated tools are
    appended after the stable core prefix, an EXTRA checkpoint on
    tools[core_count-1] keeps the big core prefix hitting across activation
    events — each activation then re-writes only (activated schemas + static
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

    Without it the entire messages array — which dominates long agentic turns —
    is re-billed as uncached input on every round of every turn. Bedrock
    resolves cache hits by longest-prefix lookback over recent breakpoint
    positions, so one checkpoint on the LAST message yields round-over-round
    incremental reads within a turn and turn-over-turn reads across turns.

    Returns a REQUEST-ONLY copy: the list is shallow-copied and only the last
    message is deep-copied before ``cache_control`` is set on its final content
    block. The shared ``messages`` list is NEVER mutated — those dicts are
    persisted to chat history and replayed by the OpenAI and local paths,
    where a stray ``cache_control`` key would fail the request or leak into
    storage. String content is converted to a text-block list as needed.

    Breakpoint budget: tools (1) + static system (1) + this (1) = 3 of the 4
    Anthropic allows, leaving one slot free (workstream G's core-tools split).
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
