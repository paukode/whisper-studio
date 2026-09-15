"""Per-session inbox for a user message sent while a turn is already running.

A NEW turn (not an approval continuation) that arrives at POST /api/chat while
the session is already streaming used to get HTTP 409 SESSION_BUSY and go
nowhere — the composer either blocked sending or the text was silently
dropped. That is not what a user typing "stop, just give me what you have" or
extra context mid-run wants: they want the RUNNING turn to take it into
account, the way Claude Code folds a message sent mid-turn into the turn
that's already in flight instead of starting a separate one.

server/chat/routes.py pushes here instead of returning 409. run_turn's
per-round loop (server/chat/engine/runner.py) drains it once per round — the
same checkpoint that already injects wind-down reminders — and folds any
pending text into the live message list via loop_hints.inject_reminder, so
the model sees it, addressed, before its next tool call.
"""

import threading

_lock = threading.Lock()
_inboxes: dict[str, list[str]] = {}


def push(session_id: str, text: str) -> None:
    """Queue a message for the turn currently running in ``session_id``."""
    text = (text or "").strip()
    if not session_id or not text:
        return
    with _lock:
        _inboxes.setdefault(session_id, []).append(text)


def drain(session_id: str) -> list[str]:
    """Return and clear every message queued for this session, in order."""
    with _lock:
        return _inboxes.pop(session_id, [])


def has_pending(session_id: str) -> bool:
    with _lock:
        return bool(_inboxes.get(session_id))
