"""Adapter contract and factory for per-turn agent model calls.

Canonical representation is the Anthropic message shape ([{role, content:
[blocks]}] with text/tool_use/tool_result/thinking blocks): the whole
existing loop, tool executor, and message injection already speak it, so
each adapter converts at the wire and nothing else changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger("whisper-studio")

# Concurrent model calls across ALL agents. The workflow runtime (workstream
# D) raises this toward its 16-way design; keep the constant here so there is
# exactly one knob.
AGENT_CALL_CONCURRENCY = 4


@dataclass
class TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def add(self, other: TurnUsage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
        }


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
