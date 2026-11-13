"""Thinking blocks that are bound to the conversation that produced them.

Claude Fable 5.2 and Mythos 5.2 check every replayed ``thinking`` block
against the system prompt, tools and messages that came before it. A request
that sends a block back after any of those changed is refused::

    ValidationException: Invalid `signature` in `thinking` block. The block
    is bound to a different conversation.

Verified live on Bedrock InvokeModel (2026-11-13): an edited system prompt
and an edited earlier message both get that 400, and text appended after the
newest tool result does not. The turn loop does rewrite earlier messages
inside one turn (server/chat/compaction.py prunes old tool results and
summarizes old rounds, keeping the recent assistant messages and their
thinking blocks), so those models are asked to drop a block that no longer
matches. The request then succeeds and the model thinks again where a block
was dropped.

Only the models that run the check get the setting: on other models it would
opt the request into a check they do not run by default.
"""

# Model-id markers of the Claude models that enforce the check.
THINKING_BINDING_MARKERS = ("fable-5-2", "mythos-5-2")

BINDING_BETA = "thinking-binding-controls-2026-08-01"


def enforces_thinking_binding(model_id: str) -> bool:
    mid = (model_id or "").lower()
    return any(marker in mid for marker in THINKING_BINDING_MARKERS)


def drop_mismatched_thinking(body: dict, model_id: str) -> None:
    """Ask the model to drop, not refuse, thinking blocks whose history changed.

    Edits ``body`` in place, and only when it carries a ``thinking`` setting
    for a model that enforces the check."""
    thinking = body.get("thinking")
    if not isinstance(thinking, dict) or not enforces_thinking_binding(model_id):
        return
    thinking["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
    betas = body.setdefault("anthropic_beta", [])
    if BINDING_BETA not in betas:
        betas.append(BINDING_BETA)
