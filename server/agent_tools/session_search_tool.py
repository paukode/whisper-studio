"""Executor for the ``session_search`` tool (see server/infrastructure/session_search.py).

Four calling shapes, inferred from the arguments (no mode parameter):

    query                          discovery: best hit per session, ranked; the top
                                   result carries a small window around its match
    session_id + around_index      scroll: a window of messages around an index
    session_id (nothing else)      read: the whole session (head and tail when long)
    (no arguments)                 browse: recent sessions with a one-line preview

Returns the actual stored messages. No model calls.
"""

from __future__ import annotations

import json

from server.infrastructure import session_search as _ss

_TOP_HIT_WINDOW = 3


def _enabled() -> bool:
    try:
        from server.infrastructure.feature_flags import is_enabled

        return is_enabled("session_search")
    except Exception:  # noqa: BLE001
        return True


def _int(value, default: int | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def execute_session_search(tool_input: dict, session_id: str = "") -> str:
    if not _enabled():
        return json.dumps({"error": "session_search is disabled (feature flag session_search)"})
    query = str(tool_input.get("query") or "").strip()
    target = str(tool_input.get("session_id") or "").strip() or None
    around = _int(tool_input.get("around_index"), None)
    window = _int(tool_input.get("window"), _ss.DEFAULT_WINDOW) or _ss.DEFAULT_WINDOW
    limit = _int(tool_input.get("limit"), _ss.DEFAULT_LIMIT) or _ss.DEFAULT_LIMIT

    if target and around is not None:
        view = _ss.read_window(target, around_index=around, window=window)
        if view is None:
            return json.dumps({"error": f"no session {target}"})
        return json.dumps({"shape": "scroll", **view})

    if target and not query:
        view = _ss.read_window(target, around_index=None, window=window)
        if view is None:
            return json.dumps({"error": f"no session {target}"})
        return json.dumps({"shape": "read", **view})

    if query:
        if not _ss.available():
            return json.dumps(
                {"error": "full-text search is unavailable in this build (SQLite without FTS5)"}
            )
        roles: tuple[str, ...] = _ss.MODEL_ROLES
        if tool_input.get("include_tool_output"):
            roles = (*roles, "tool")
        hits = _ss.search(query, limit=limit, session_id=target, roles=roles)
        results = []
        for i, hit in enumerate(hits):
            entry = {
                **hit,
                "is_current_session": hit["session_id"] == session_id,
            }
            # Title and transcript hits point at no chat message; the window
            # around a message index only makes sense for message hits.
            in_messages = 0 <= hit["msg_index"] < _ss.TRANSCRIPT_BASE
            if not in_messages:
                entry["hit_kind"] = "title" if hit["msg_index"] == _ss.TITLE_INDEX else "transcript"
            if i == 0:
                win = _ss.read_window(
                    hit["session_id"],
                    around_index=hit["msg_index"] if in_messages else None,
                    window=_TOP_HIT_WINDOW,
                )
                if win:
                    entry["messages"] = win["messages"]
                    entry["messages_before"] = win["messages_before"]
                    entry["messages_after"] = win["messages_after"]
            results.append(entry)
        return json.dumps(
            {
                "shape": "discovery",
                "query": query,
                "results": results,
                "count": len(results),
                "hint": (
                    "Scroll a result with session_id + around_index (its msg_index), or read a "
                    "whole session with session_id alone."
                ),
            }
        )

    return json.dumps({"shape": "browse", "sessions": _ss.browse(limit=limit)})


__all__ = ["execute_session_search"]
