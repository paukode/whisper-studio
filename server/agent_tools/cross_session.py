"""Cross-session messaging: list_sessions / send_session_message.

Sibling to spawn.py's send_message/list_agents, which mailbox agents WITHIN
one session. These two instead reach between independent chat sessions
(separate tabs/threads), reusing the exact persist-and-publish path cron
already uses (server/tasks/events.py:emit_session_event) so a message lands
in the target session's chat_history and, if that session's tab is open,
shows up live over its event-bus subscription too. No socket, no registry:
it's one local server process, so the address is just the session id.
"""

import hashlib
import json
import threading
import time

# How long an identical (sender, target, content) triple is suppressed for.
# Mirrors Claude Code's own "drops identical repeats within a short window"
# loop protection — without this, two sessions echoing the same finding back
# and forth would resend on every turn.
_DEDUPE_WINDOW_SECONDS = 15.0
_MAX_TRACKED = 500

_recent_sends: dict[tuple[str, str, str], float] = {}
_recent_sends_lock = threading.Lock()


def _is_duplicate(from_id: str, to_id: str, content: str) -> bool:
    digest = hashlib.sha1(content.encode("utf-8", "ignore")).hexdigest()
    key = (from_id, to_id, digest)
    now = time.time()
    with _recent_sends_lock:
        last = _recent_sends.get(key)
        if last is not None and now - last < _DEDUPE_WINDOW_SECONDS:
            return True
        _recent_sends[key] = now
        if len(_recent_sends) > _MAX_TRACKED:
            cutoff = now - _DEDUPE_WINDOW_SECONDS
            for k, ts in list(_recent_sends.items()):
                if ts < cutoff:
                    del _recent_sends[k]
    return False


def execute_list_sessions(session_id: str) -> str:
    """List the user's other chat sessions (not in-process agents).

    Excludes the caller's own session, the pinned cron inbox, and archived
    sessions — none of those are sensible send_session_message targets.
    """
    from server.infrastructure.sessions import CRON_INBOX_ID, _get_conn, _row_to_summary

    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
    summaries = (_row_to_summary(r) for r in rows)
    sessions = [
        {"session_id": s["id"], "title": s["title"], "last_active": s["date"]}
        for s in summaries
        if s["id"] not in (session_id, CRON_INBOX_ID) and not s["archived"]
    ]
    return json.dumps({"sessions": sessions, "count": len(sessions)})


def execute_send_session_message(tool_input: dict, session_id: str) -> str:
    """Deliver a text message into another session's chat_history.

    Delivery is "wait" style, matching how cron_event already lands in an
    idle session: the row persists and publishes live if that session's tab
    is open, but nothing wakes an idle session up to act on it immediately.
    """
    from server.infrastructure.sessions import get_session_meta
    from server.tasks.events import emit_session_event

    to_session_id = (tool_input.get("to_session_id") or "").strip()
    content = (tool_input.get("content") or "").strip()
    if not to_session_id or not content:
        return json.dumps({"error": "specify to_session_id and content"})
    if to_session_id == session_id:
        return json.dumps({"error": "cannot message your own session"})

    target = get_session_meta(to_session_id)
    if target is None:
        return json.dumps({"error": f"no such session: {to_session_id}"})
    if target.get("archived"):
        return json.dumps({"error": "target session is archived"})

    if _is_duplicate(session_id, to_session_id, content):
        return json.dumps({"error": "identical message already sent to this session moments ago"})

    from_meta = get_session_meta(session_id) if session_id else None
    from_title = (from_meta or {}).get("title") or "another session"

    emit_session_event(
        to_session_id,
        role="session_message",
        payload_key="sessionMessage",
        payload={
            "from_session_id": session_id,
            "from_title": from_title,
            "content": content,
        },
    )
    return json.dumps({"sent": True, "to": to_session_id, "to_title": target.get("title")})
