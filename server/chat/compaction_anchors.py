"""Deterministic companions to the LLM compaction summary.

A summary paraphrases. Two kinds of content must never be paraphrased:

* The user's own messages. Assistant narration ("I read X, ran Y, saw Z")
  compresses with little loss, but the user's instructions are the source of
  truth the rest of the work derives from; a summary that turns "use the
  existing retry helper, do not add a new one" into "discussed retries" is
  how an agent confidently does the forbidden thing six turns later. So the
  real user prompts from the summarized region ride along verbatim, newest
  first, within a budget.
* Identifiers. File paths, PR and issue numbers, commit SHAs, URLs, and
  error strings are extracted mechanically with regexes and listed as an
  anchor index, exactly as they appeared.

Both blocks are appended to whatever summary message compaction produces
(the LLM summary or the session-memory shortcut), together with a recovery
pointer: the persisted session history still holds every message, and the
``session_search`` tool can pull any of them back verbatim.
"""

from __future__ import annotations

import re

USER_BUDGET_CHARS = 4000
USER_PER_MESSAGE_CHARS = 800
ANCHOR_MAX_ITEMS = 40
ANCHOR_MAX_CHARS = 2500
_ANCHOR_SCAN_PER_MESSAGE = 6000

# Prefixes the loop itself writes into user-role rows; not the user's words.
_SYNTHETIC_PREFIXES = (
    "[completion gate]",
    "[Context summary",
    "[The conversation no longer fits",
    "<system-reminder>",
    "<user_message_mid_turn>",
    "Continue exactly where you left off",
    "[Hook context]",
)

_PATH_RE = re.compile(r"(?<![\w@])(?:~|/)[\w.\-]+(?:/[\w.\-]+)+")
_PR_RE = re.compile(r"(?<![\w&])#\d{1,6}\b|\bPR\s?#?\d{1,6}\b")
_SHA_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
_URL_RE = re.compile(r"https?://[^\s)\]\"'<>]+")
_ERROR_RE = re.compile(
    r"^[^\n]*(?:\b\w*(?:Error|Exception)\b|\bTraceback\b|\bFAILED\b)[^\n]*$", re.MULTILINE
)


def _text_of(content, *, include_tool_results: bool) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(str(b.get("text", "")))
        elif t == "tool_result" and include_tool_results:
            inner = b.get("content", "")
            if isinstance(inner, list):
                inner = " ".join(
                    str(x.get("text", "")) for x in inner if isinstance(x, dict) and x.get("text")
                )
            parts.append(str(inner))
    return "\n".join(p for p in parts if p)


def _is_real_user_prompt(msg: dict) -> bool:
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        return False
    text = _text_of(content, include_tool_results=False).strip()
    if not text:
        return False
    return not text.startswith(_SYNTHETIC_PREFIXES)


def verbatim_user_messages(
    messages: list,
    *,
    budget_chars: int = USER_BUDGET_CHARS,
    per_message_chars: int = USER_PER_MESSAGE_CHARS,
) -> list[str]:
    """The user's real prompts from ``messages``, newest first, each capped at
    ``per_message_chars`` and the whole list at ``budget_chars``."""
    out: list[str] = []
    used = 0
    for msg in reversed(messages):
        if not _is_real_user_prompt(msg):
            continue
        text = _text_of(msg.get("content"), include_tool_results=False).strip()
        if len(text) > per_message_chars:
            text = text[:per_message_chars].rstrip() + " ..."
        if used + len(text) > budget_chars:
            break
        out.append(text)
        used += len(text)
    return out


def extract_anchors(
    messages: list,
    *,
    max_items: int = ANCHOR_MAX_ITEMS,
    max_chars: int = ANCHOR_MAX_CHARS,
) -> list[str]:
    """Identifiers found in ``messages`` (paths, PR numbers, SHAs, URLs, error
    lines), verbatim, deduplicated, in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    total = 0

    def _add(item: str) -> bool:
        nonlocal total
        item = item.strip().rstrip(".,;:")
        if not item or item in seen:
            return True
        if len(out) >= max_items or total + len(item) > max_chars:
            return False
        seen.add(item)
        out.append(item)
        total += len(item)
        return True

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        text = _text_of(msg.get("content"), include_tool_results=True)[:_ANCHOR_SCAN_PER_MESSAGE]
        if not text:
            continue
        url_spans: list[tuple[int, int]] = []
        for m in _URL_RE.finditer(text):
            url_spans.append(m.span())
            if not _add(m.group(0)):
                return out
        for m in _PATH_RE.finditer(text):
            # A URL's path component is not a filesystem path.
            if any(lo <= m.start() < hi for lo, hi in url_spans):
                continue
            if not _add(m.group(0)):
                return out
        for m in _PR_RE.finditer(text):
            digits = re.search(r"\d+", m.group(0))
            if digits and not _add(f"#{digits.group(0)}"):
                return out
        for m in _SHA_RE.finditer(text):
            if not _add(m.group(0)):
                return out
        for m in _ERROR_RE.finditer(text):
            line = m.group(0).strip()
            if len(line) > 160:
                line = line[:160].rstrip() + " ..."
            if not _add(line):
                return out
    return out


def recovery_pointer(session_id: str) -> str:
    return (
        "Recovery: every summarized message is still stored verbatim in this session's "
        f'history. Call session_search with session_id="{session_id}" and a query (or an '
        "around_index) to read any of it back before asking the user to repeat themselves."
    )


def render_summary_extras(old_messages: list, session_id: str = "") -> str:
    """The deterministic block appended to a compaction summary message."""
    sections: list[str] = []
    users = verbatim_user_messages(old_messages)
    if users:
        body = "\n".join(f"- {u}" for u in users)
        sections.append(
            f"## User messages from the summarized region (verbatim, newest first)\n{body}"
        )
    anchors = extract_anchors(old_messages)
    if anchors:
        body = "\n".join(f"- {a}" for a in anchors)
        sections.append(f"## Anchors (mechanically extracted, exact)\n{body}")
    if session_id:
        sections.append(recovery_pointer(session_id))
    return "\n\n".join(sections)


__all__ = [
    "ANCHOR_MAX_CHARS",
    "ANCHOR_MAX_ITEMS",
    "USER_BUDGET_CHARS",
    "USER_PER_MESSAGE_CHARS",
    "extract_anchors",
    "recovery_pointer",
    "render_summary_extras",
    "verbatim_user_messages",
]
