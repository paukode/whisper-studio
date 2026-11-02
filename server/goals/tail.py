"""Provider-neutral transcript-tail renderer for the goal evaluator.

The evaluator judges a rendered TEXT tail, never the raw messages array, so
the input stays token-bounded. Tool results are head+tail sliced (same
philosophy as server/chat/budget.py) so a giant file dump can't blow the cap.
"""

from __future__ import annotations

DEFAULT_MAX_MESSAGES = 15
DEFAULT_CAP_CHARS = 12_000
_TOOL_RESULT_SLICE = 800  # head+tail chars kept per tool result
# Prefixed to a tail cut at ``cap_chars``, so a caller sizing a prompt adds it.
ELIDED_PREFIX = "… [older turns elided] …\n"


def _slice(text: str, budget: int = _TOOL_RESULT_SLICE) -> str:
    text = text or ""
    if len(text) <= budget:
        return text
    half = budget // 2
    return f"{text[:half]}\n… [{len(text) - budget} chars elided] …\n{text[-half:]}"


def _render_blocks(content) -> str:
    """Flatten one message's content (a string or content blocks) to a
    readable line."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for b in content:
        if not isinstance(b, dict):
            parts.append(str(b))
            continue
        btype = b.get("type", "")
        if btype == "text":
            parts.append(b.get("text", ""))
        elif btype == "tool_use":
            parts.append(f"[tool_use {b.get('name', '?')}]")
        elif btype == "tool_result":
            raw = b.get("content", "")
            if isinstance(raw, list):
                raw = " ".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in raw)
            parts.append(f"[tool_result] {_slice(str(raw))}")
        elif b.get("text"):
            parts.append(b["text"])
    return " ".join(p for p in parts if p)


def render_tail(
    messages: list,
    *,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    cap_chars: int = DEFAULT_CAP_CHARS,
) -> str:
    """Render the last ``max_messages`` messages to a text transcript, capped at
    ``cap_chars`` (keeping the most recent content)."""
    if not messages:
        return "(empty transcript)"
    lines: list[str] = []
    for m in messages[-max_messages:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "?")
        body = _render_blocks(m.get("content", ""))
        if body.strip():
            lines.append(f"{role.upper()}: {body.strip()}")
    text = "\n".join(lines)
    if len(text) > cap_chars:
        text = ELIDED_PREFIX + text[-cap_chars:]
    return text or "(no textual content)"
