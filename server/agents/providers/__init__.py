"""Provider adapters for the agent runtime.

One seam, one contract: ``get_adapter(model_key, model_id).invoke(...)``
returns a :class:`ProviderTurn` in the canonical Anthropic message shape,
so the rest of the agent loop (tool routing, approval handling, messaging,
events) stays provider-neutral. Anthropic-on-Bedrock and OpenAI-on-Bedrock
(Responses API via bedrock-mantle) are supported today.
"""

from server.agents.providers.base import (
    AGENT_CALL_CONCURRENCY,
    ModelAdapter,
    ProviderTurn,
    TurnUsage,
    get_adapter,
    model_key_for_id,
)

__all__ = [
    "AGENT_CALL_CONCURRENCY",
    "ModelAdapter",
    "ProviderTurn",
    "TurnUsage",
    "get_adapter",
    "model_key_for_id",
]
