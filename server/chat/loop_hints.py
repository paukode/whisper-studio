"""Loop hygiene hints: near-cap wind-down and token-aware context tracking.

Two long-standing loop gaps close here:

1. The model was never told it was approaching the tool-round cap — round 50
   simply cut it off mid-plan with "(Reached maximum tool rounds)". Now it
   gets a wind-down reminder with 5 rounds left and a final-round notice, so
   it consolidates instead of opening new work.

2. Context accounting was character-based guesswork (COMPACT_TRIGGER_CHARS).
   Every round's message_start usage already reports the TRUE prompt size
   (input + cache_read + cache_creation tokens); recording it per session
   gives a real context meter (the Stats panel bar) and an early compaction
   nudge at 80% of the model's window, both at zero added cost.

Reminders are PERSISTED into history (appended to the last user message):
injecting request-only would fork the token prefix between rounds and destroy
the moving cache checkpoint's hits. Two small blocks per capped turn is
negligible bloat, and they are honest context for later turns anyway.
"""

import logging
import threading

log = logging.getLogger("whisper-studio")

DEFAULT_CONTEXT_MAX = 200_000
COMPACT_NUDGE_FRACTION = 0.8

WIND_DOWN_AT = 5
FINAL_ROUND_AT = 1

_lock = threading.Lock()
# session_id -> {"used": int, "max": int, "nudged": bool}
_context: dict[str, dict] = {}
_MAX_SESSIONS = 512


def context_window_for(model_key: str) -> int:
    """Context window for a model key, with a config override map."""
    try:
        from server.infrastructure.config import load_config

        windows = load_config().get("context_windows") or {}
        v = windows.get(model_key)
        if isinstance(v, int) and v > 0:
            return v
    except Exception:
        pass
    return DEFAULT_CONTEXT_MAX


def note_prompt_tokens(session_id: str, tokens: int, context_max: int | None = None) -> None:
    """Record the latest round's true prompt size for a session."""
    if not session_id or tokens <= 0:
        return
    with _lock:
        if len(_context) >= _MAX_SESSIONS and session_id not in _context:
            _context.pop(next(iter(_context)), None)
        # Pop+reinsert makes the dict order LRU-by-update, so eviction
        # targets the least-recently-ACTIVE session, never the busy one
        # (plain setdefault would leave order FIFO-by-first-insert).
        entry = _context.pop(session_id, None) or {
            "used": 0,
            "max": DEFAULT_CONTEXT_MAX,
            "nudged": False,
        }
        entry["used"] = tokens
        if context_max:
            entry["max"] = context_max
        _context[session_id] = entry


def context_estimate(session_id: str) -> tuple[int | None, int]:
    """Latest known (context_used, context_max) for a session."""
    with _lock:
        entry = _context.get(session_id)
        if not entry:
            return None, DEFAULT_CONTEXT_MAX
        return entry["used"], entry["max"]


def should_nudge_compaction(session_id: str) -> bool:
    """True ONCE per session when the real prompt size crosses the nudge
    fraction of the window — lets proactive compaction fire on token truth
    even when the char estimate is still under COMPACT_TRIGGER_CHARS."""
    with _lock:
        entry = _context.get(session_id)
        if not entry or entry["nudged"]:
            return False
        if entry["used"] >= entry["max"] * COMPACT_NUDGE_FRACTION:
            entry["nudged"] = True
            return True
        return False


def reset_session(session_id: str) -> None:
    with _lock:
        _context.pop(session_id, None)


def near_cap_reminder(rounds_left: int) -> str | None:
    """Wind-down text at exactly two thresholds; None otherwise."""
    if rounds_left == WIND_DOWN_AT:
        return (
            "<system-reminder>Only "
            f"{WIND_DOWN_AT} tool rounds remain in this turn. Consolidate: "
            "finish what is in flight, do not open new lines of work, and "
            "prepare your final answer.</system-reminder>"
        )
    if rounds_left == FINAL_ROUND_AT:
        return (
            "<system-reminder>This is the final tool round. Answer now with "
            "what you have; further tool calls will not execute.</system-reminder>"
        )
    return None


def inject_reminder(messages: list[dict], text: str) -> bool:
    """Append a reminder text block to the LAST user message in place.

    Returns True when injected. Only user-role tails are eligible (that is
    where the loop sits when it is about to call the model again).
    """
    if not messages or not text:
        return False
    last = messages[-1]
    if last.get("role") != "user":
        return False
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [{"type": "text", "text": content}] if content else []
        content = last["content"]
    if not isinstance(content, list):
        return False
    content.append({"type": "text", "text": text})
    return True
