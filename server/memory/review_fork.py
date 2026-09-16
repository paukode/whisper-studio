"""Post-turn learning review as a cache-parity fork of the live turn.

The old extractor re-sent a 20-message, 500-char-per-message excerpt of the
conversation to a separate agent with its own system prompt: a cold request
that paid full input price for a lossy transcript. The fork instead replays
the turn's OWN request: the same adapter (same system prompt, same cache
breakpoints), the same tools array byte for byte, the same messages, plus one
appended user message asking "what should be remembered or encoded as a
skill?". Every token before that message is a warm prompt-cache read, and the
reviewer sees the full conversation, not an excerpt.

Only memory and skill tools (and read-only workspace reads) may run inside
the fork; anything else the model calls gets a refusal tool_result. The fork
never touches the live conversation, never emits SSE, and records its usage
against the session with a distinct turn number so the cost is visible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("whisper-studio")

MAX_ROUNDS = 6
REVIEW_TURN_BASE = 900  # cost rows for fork rounds are numbered from here

ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "memory_read",
        "memory_write",
        "memory_list",
        "memory_delete",
        "skill_list",
        "skill_manage",
        "ws_read_file",
        "ws_grep",
        "ws_glob",
    }
)

_FORK_PROVIDERS = frozenset({"anthropic", "openai"})


@dataclass
class ReviewFork:
    """Everything needed to replay the turn's request with one extra message."""

    adapter: Any
    tools: list
    core_count: int | None
    messages: list  # the turn's final message list INCLUDING the last assistant reply
    model_key: str
    model_id: str
    session_id: str
    ws_path: str | None
    loop: Any
    executor: Any
    transcript: str = ""
    attachments: dict = field(default_factory=dict)


def supports_fork(adapter: Any) -> bool:
    return getattr(adapter, "provider", "") in _FORK_PROVIDERS


def count_user_prompts(messages: list, since_index: int = 0) -> int:
    from server.chat.compaction_anchors import _is_real_user_prompt

    return sum(1 for m in messages[max(0, since_index) :] if _is_real_user_prompt(m))


async def _dispatch(fork: ReviewFork, tool_use: dict) -> str:
    from server.tool_router import route_tool

    name = str(tool_use.get("name") or "")
    if name not in ALLOWED_TOOLS:
        return (
            f"[Learning review] '{name}' is not available during the post-turn review; only "
            "memory tools, skill tools and read-only workspace reads are. Save what you learned "
            "or reply 'Nothing to save.'"
        )
    call_input = dict(tool_use.get("input") or {})
    call_input["__session_id__"] = fork.session_id
    call_input["__agent__"] = True
    try:
        output, _effects = await route_tool(
            name,
            call_input,
            loop=fork.loop,
            executor=fork.executor,
            transcript=fork.transcript,
            attachments=fork.attachments,
            session_id=fork.session_id,
            model_id=fork.model_id,
            tool_use_id=str(tool_use.get("id") or ""),
            origin="review",
        )
    except Exception as e:  # noqa: BLE001 - a tool bug must not kill the review
        return f"[Tool Error] {e}"
    return output if isinstance(output, str) else str(output)


def _record_usage(fork: ReviewFork, round_num: int, usage: Any) -> None:
    try:
        from server.chat.infra import _estimate_cost
        from server.costs.tracker import record_turn

        cached_in_input = getattr(fork.adapter, "cached_in_input", False)
        cost = _estimate_cost(
            fork.model_key,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
            cached_in_input=cached_in_input,
        )
        record_turn(
            session_id=fork.session_id,
            turn_number=REVIEW_TURN_BASE + round_num,
            model=fork.model_key,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_write_tokens,
        )
    except Exception as e:  # noqa: BLE001
        log.debug("review fork usage not recorded: %s", e)


async def run_review(
    fork: ReviewFork,
    *,
    since_index: int = 0,
    review_skills: bool = True,
    project_scope: bool = False,
    max_rounds: int = MAX_ROUNDS,
) -> dict:
    """Replay the turn with one appended review message and let the model
    call memory and skill tools. Returns a small stats dict."""
    from server.chat.budget import make_budget_tool_result
    from server.chat.engine.events import RoundError, RoundResult
    from server.memory.prompts import build_learning_review_prompt

    prompt = build_learning_review_prompt(
        recent_user_turns=count_user_prompts(fork.messages, since_index),
        review_skills=review_skills,
        project_scope=project_scope,
    )
    messages = list(fork.messages) + [{"role": "user", "content": prompt}]
    budget = make_budget_tool_result([])
    stats: dict[str, Any] = {
        "rounds": 0,
        "tool_calls": [],
        "cache_read_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "ended": "no_result",
    }
    for round_num in range(max_rounds):
        result: RoundResult | None = None
        try:
            async for ev in fork.adapter.stream_round(
                messages, fork.tools, fork.core_count, REVIEW_TURN_BASE + round_num, False
            ):
                if isinstance(ev, RoundResult):
                    result = ev
                elif isinstance(ev, RoundError):
                    log.warning("learning review round %d failed: %s", round_num, ev.message)
                    stats["ended"] = "error"
                    return stats
        except Exception as e:  # noqa: BLE001 - never propagate into the turn's aftermath
            log.warning("learning review aborted: %s", e)
            stats["ended"] = "error"
            return stats
        if result is None:
            return stats
        stats["rounds"] += 1
        stats["input_tokens"] += result.usage.input_tokens
        stats["output_tokens"] += result.usage.output_tokens
        stats["cache_read_tokens"] += result.usage.cache_read_tokens
        _record_usage(fork, round_num, result.usage)
        tool_uses = [
            b for b in result.content if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        if result.stop_reason != "tool_use" or not tool_uses:
            stats["ended"] = "done"
            return stats
        messages.append({"role": "assistant", "content": result.content})
        tool_results = []
        for tu in tool_uses:
            output = await _dispatch(fork, tu)
            stats["tool_calls"].append(tu.get("name"))
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.get("id"),
                    "content": budget(str(tu.get("name") or ""), output),
                }
            )
        messages.append({"role": "user", "content": tool_results})
    stats["ended"] = "round_cap"
    return stats


__all__ = [
    "ALLOWED_TOOLS",
    "MAX_ROUNDS",
    "REVIEW_TURN_BASE",
    "ReviewFork",
    "count_user_prompts",
    "run_review",
    "supports_fork",
]
