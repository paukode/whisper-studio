"""Carrying a turn on past an apparent end.

A round can stop where the turn is not over: at the output token cap, or on
an answer the completion gate or a late mid-turn message sends back. The
loop then re-sends the round's assistant content and a user row, and both
must be shaped so the provider accepts them.
"""

from __future__ import annotations

# What the loop sends when a round stopped at the output token cap. Its first
# sentence marks the row as the engine's (deliverables._CONTINUE_PREFIX,
# compaction_anchors).
CONTINUE_AFTER_MAX_TOKENS = (
    "Continue exactly where you left off. Do not repeat anything. "
    "IMPORTANT: If you were in the middle of a code block (```html or similar), "
    "continue the code directly: do NOT close and reopen the fence, do NOT add explanation "
    "text before or inside the code. Just continue the code from the exact point it was cut "
    "off. The output will be concatenated to your previous response."
)


def strip_partial_tool_use(content: list[dict]) -> list[dict]:
    """Prepare an assistant turn for re-injection without a tool_result.

    ``max_tokens`` (or a completion-gate loop) can leave partial ``tool_use``
    blocks in the assistant turn; feeding them back without matching
    tool_results is a non-retryable provider error. Drop them, keeping the
    text/thinking, and never return an empty turn."""
    if not any(b.get("type") == "tool_use" for b in content):
        return content
    kept = [b for b in content if b.get("type") != "tool_use"]
    return kept or [{"type": "text", "text": "(continuing)"}]


def continuable_assistant(content: list[dict]) -> list[dict]:
    """The round's assistant content, shaped so the turn can carry on after it.

    Used wherever the loop decides an apparent end-of-turn is not the end (a
    completion-gate block, a mid-turn message that landed after the last
    drain). Partial tool_use blocks go, and a turn with no usable text gets a
    placeholder: some providers reject an assistant turn that is empty or
    text-less, which would kill the very turn we are trying to continue."""
    assistant = strip_partial_tool_use(content)
    has_text = isinstance(assistant, list) and any(
        isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
        for b in assistant
    )
    return assistant if has_text else [{"type": "text", "text": "(continuing)"}]
