"""Per-session activation registry for deferred tools.

In-memory and lock-guarded (the `_paused_sessions` pattern): a tool_search
hit activates tools for the session, and because the chat loop reassembles
the pool every round, an activation in round N is advertised in round N+1
with no other plumbing.

Durability without persistence: `activate_from_history` re-derives the set
from the conversation itself at turn start — any tool the model already
called in visible history re-activates in first-seen order. Restarts and
approval pauses are self-healing, activation order stays deterministic
(cache-friendly), and nothing touches the database.
"""

import logging
import threading

log = logging.getLogger("whisper-studio")

_lock = threading.Lock()
# session_id -> append-only ordered activations
_activated: dict[str, list[str]] = {}
# session_id -> version counter (bumped on change; the OpenAI loop polls it)
_versions: dict[str, int] = {}
_MAX_SESSIONS = 512


def activate(session_id: str, names: list[str]) -> list[str]:
    """Activate tools for a session; returns the names newly added."""
    if not session_id or not names:
        return []
    with _lock:
        if len(_activated) >= _MAX_SESSIONS and session_id not in _activated:
            victim = next(iter(_activated))
            _activated.pop(victim, None)
            _versions.pop(victim, None)
        current = _activated.pop(session_id, [])
        added = []
        for name in names:
            if name and name not in current:
                current.append(name)
                added.append(name)
        _activated[session_id] = current  # reinsert: LRU-by-update order
        if added:
            _versions[session_id] = _versions.get(session_id, 0) + 1
        return added


def get_ordered(session_id: str) -> list[str]:
    with _lock:
        return list(_activated.get(session_id, ()))


def version(session_id: str) -> int:
    with _lock:
        return _versions.get(session_id, 0)


def clear(session_id: str) -> None:
    with _lock:
        _activated.pop(session_id, None)
        _versions.pop(session_id, None)


def activate_from_history(session_id: str, messages: list[dict]) -> list[str]:
    """Re-activate every tool name the model already used in this history.

    Scans assistant tool_use blocks in first-seen order. Bedrock does not
    require historical tool_use names to be in the current tools array, but
    a tool the model already reached for should stay callable regardless of
    server restarts or approval-pause resumes.
    """
    if not session_id or not messages:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name", "")
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
    return activate(session_id, names)
