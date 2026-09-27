"""Adapter contract and factory for per-turn agent model calls.

Canonical representation is the Anthropic message shape ([{role, content:
[blocks]}] with text/tool_use/tool_result/thinking blocks): the whole
existing loop, tool executor, and message injection already speak it, so
each adapter converts at the wire and nothing else changes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger("whisper-studio")

# Concurrent model calls across ALL agents — one knob, deliberately equal to
# server.workflows.runtime.WORKFLOW_MAX_CONCURRENCY.
#
# It used to be 4 while the workflow scheduler admitted 16, so ULTRACODE.md's
# "agents run 16 at a time" was never true on the Anthropic path: 16 agents
# were dispatched and 12 of them sat blocked waiting for a thread. Matching
# the two makes the documented number the real one. Safe at this width
# because the shared Bedrock client uses adaptive retries (see
# server/chat/infra.py) — a burst that trips the account's quota is paced and
# retried rather than failed.
AGENT_CALL_CONCURRENCY = 16


@dataclass
class TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    # Each call priced at its own tier (tracker.call_cost) when it was made:
    # the summed counts above can cross a long-context threshold no single
    # call did, so they are never priced again.
    cost_usd: float = 0.0

    def add(self, other: TurnUsage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cost_usd += other.cost_usd

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_counts(cls, counts: dict, model_key: str) -> TurnUsage:
        """One call's usage from a server.costs.capture count dict (reported
        or estimated), priced for ``model_key``."""
        from server.costs.tracker import call_cost

        return cls(
            input_tokens=counts["input_tokens"],
            output_tokens=counts["output_tokens"],
            cache_read_tokens=counts["cache_read_tokens"],
            cache_creation_tokens=counts["cache_write_tokens"],
            cost_usd=call_cost(
                model_key,
                counts["input_tokens"],
                counts["output_tokens"],
                counts["cache_read_tokens"],
                counts["cache_write_tokens"],
            ),
        )


# Called once per billed call with its server.costs.capture counts: the
# payload's, or characters / 4 of what was posted and received. It runs where
# the call ends (the worker thread for invoke_model), so a call its awaiting
# coroutine was cancelled on is still reported.
CountsHook = Callable[[dict], None]


@dataclass
class ProviderTurn:
    """One model turn, provider-normalized.

    ``assistant_blocks`` are canonical content blocks to append to history —
    for Anthropic the raw blocks INCLUDING thinking and redacted_thinking
    (dropping redacted_thinking breaks multi-turn replay once adaptive
    thinking is on); for OpenAI, synthesized text + tool_use blocks carrying
    the Responses call_id as the block id so tool_result blocks round-trip.
    ``tool_calls`` are the tool_use blocks only (a subset of
    assistant_blocks), in emission order.
    """

    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    assistant_blocks: list[dict] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: TurnUsage = field(default_factory=TurnUsage)
    structured_output: dict | None = None


class ModelAdapter(Protocol):
    async def invoke(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict] | None,
        max_tokens: int,
        effort_label: str | None = None,
        force_structured: dict | None = None,
        on_counts: CountsHook | None = None,
    ) -> ProviderTurn: ...


def model_key_for_id(model_id: str) -> str:
    """Reverse lookup id -> chat_models key ('' when unknown)."""
    if not model_id:
        return ""
    try:
        from server.infrastructure.config import load_config

        for key, mid in (load_config().get("chat_models") or {}).items():
            if mid == model_id:
                return key
    except Exception:
        pass
    return ""


def _is_openai(model_key: str, model_id: str) -> bool:
    try:
        from server.openai_bedrock.runtime import is_openai_model

        if model_key and is_openai_model(model_key):
            return True
    except Exception:
        pass
    return "openai" in (model_id or "").lower()


def get_adapter(model_key: str, model_id: str) -> ModelAdapter:
    """Adapter for a model. Provider comes from chat_model_meta via the
    model key (same predicate the chat route uses), with an id-substring
    fallback for callers that only have the raw Bedrock id."""
    if not model_key:
        model_key = model_key_for_id(model_id)
    if _is_openai(model_key, model_id):
        from server.agents.providers.openai import OpenAIBedrockAdapter

        return OpenAIBedrockAdapter(model_key=model_key, model_id=model_id)
    from server.agents.providers.anthropic import AnthropicBedrockAdapter

    return AnthropicBedrockAdapter(model_key=model_key, model_id=model_id)
