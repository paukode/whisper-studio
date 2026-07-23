"""Provider-neutral transcript-tail renderer for the goal evaluator.

The evaluator judges a rendered TEXT tail, never the raw messages array, so the
same code handles Anthropic content blocks and OpenAI Responses input items and
the input stays token-bounded. Tool results are head+tail sliced (same
philosophy as server/chat/budget.py) so a giant file dump can't blow the cap.
"""

from __future__ import annotations

DEFAULT_MAX_MESSAGES = 15
DEFAULT_CAP_CHARS = 12_000
_TOOL_RESULT_SLICE = 800  # head+tail chars kept per tool result


def _slice(text: str, budget: int = _TOOL_RESULT_SLICE) -> str:
    text = text or ""
    if len(text) <= budget:
        return text
    half = budget // 2
    return f"{text[:half]}\n… [{len(text) - budget} chars elided] …\n{text[-half:]}"


def _render_blocks(content) -> str:
    """Flatten one message's content (string, Anthropic blocks, or Responses
    items) to a readable line."""
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
        if btype in ("text", "input_text", "output_text"):
            parts.append(b.get("text", ""))
        elif btype == "tool_use":
            parts.append(f"[tool_use {b.get('name', '?')}]")
        elif btype == "function_call":
            parts.append(f"[tool_use {b.get('name', '?')}]")
        elif btype in ("tool_result", "function_call_output"):
            raw = b.get("content", b.get("output", ""))
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
        # OpenAI Responses input lists carry tool activity as TOP-LEVEL items
        # ({"type": "function_call", ...} with no role/content) — render them
        # so GPT sessions' tool calls/results are visible to the evaluator.
        mtype = m.get("type", "")
        if mtype == "function_call":
            lines.append(f"ASSISTANT: [tool_use {m.get('name', '?')}]")
            continue
        if mtype == "function_call_output":
            out = m.get("output", m.get("content", ""))
            if isinstance(out, list):
                out = " ".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in out)
            lines.append(f"TOOL: [tool_result] {_slice(str(out))}")
            continue
        role = m.get("role", "?")
        body = _render_blocks(m.get("content", ""))
        if body.strip():
            lines.append(f"{role.upper()}: {body.strip()}")
    text = "\n".join(lines)
    if len(text) > cap_chars:
        text = "… [older turns elided] …\n" + text[-cap_chars:]
    return text or "(no textual content)"
