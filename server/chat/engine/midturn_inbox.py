"""Per-session inbox for text that reaches a chat turn while it is running.

A NEW turn (not an approval continuation) that arrives at POST /api/chat while
the session is already streaming used to get HTTP 409 SESSION_BUSY and go
nowhere: the composer either blocked sending or the text was silently
dropped. That is not what a user typing "stop, just give me what you have" or
extra context mid-run wants: they want the RUNNING turn to take it into
account, the way Claude Code folds a message sent mid-turn into the turn
that's already in flight instead of starting a separate one.

Two producers push here:

- server/chat/routes.py, with the user's text (kind ``user``);
- server/agents/wake.py, with agent reports that land while a chat turn is
  live (kind ``agent_report``). The runner frames those as agent output, never
  as something the user said.

Exactly one consumer drains it: the user-facing chat turn that owns the
session's busy slot (``TurnContext.midturn_inbox``). Subagents, cron, voice
and wake turns share the chat session id but never read or wait on it.
run_turn drains it once per round and folds the text into the live message
list, persisted like every other mid-turn reminder.

Lifecycle, driven by the slot owner:

- a fresh chat turn claims the slot and clears whatever is left: every older
  entry is already in the history the client sent with this turn;
- an approval continuation keeps the entries, since it is the same turn;
- the runner closes the inbox at its final look (``close_if_empty``, then
  ``close_and_take`` on every exit that reads no more), in the same
  synchronous step that ends the turn, so a push that comes after it is
  refused and the caller starts the next turn instead of queueing text that
  nobody will read. What was accepted but never read is handed on: agent
  reports get a wake turn, and a user message is announced as unanswered;
- a turn that is stopped or disconnected clears it, as does /reset.

A turn that ends before its round loop runs (a failed or cancelled local
model load, a setup error, a refusal) takes the same last look
(``close_and_announce``) right before its [DONE].
"""

import threading
from typing import NamedTuple

USER = "user"
AGENT_REPORT = "agent_report"


class Entry(NamedTuple):
    kind: str
    text: str
    # The report payload behind an agent_report entry, so reports the turn
    # never read can still be handed to a wake turn.
    payload: dict | None = None


_lock = threading.Lock()
_inboxes: dict[str, list[Entry]] = {}
# Sessions whose running turn has taken its last look at the inbox.
_closed: set[str] = set()


def push(session_id: str, text: str, kind: str = USER, payload: dict | None = None) -> bool:
    """Queue text for the turn currently running in ``session_id``.

    Returns False when that turn is ending and no longer reads the inbox; the
    caller must then treat the session as idle. A blank text needs no
    delivery and is not queued, but still returns True."""
    text = (text or "").strip()
    if not session_id:
        return False
    with _lock:
        if session_id in _closed:
            return False
        if text:
            _inboxes.setdefault(session_id, []).append(Entry(kind, text, payload))
        return True


def drain(session_id: str) -> list[Entry]:
    """Return and clear every entry queued for this session, in order."""
    with _lock:
        return _inboxes.pop(session_id, [])


def has_pending(session_id: str) -> bool:
    with _lock:
        return bool(_inboxes.get(session_id))


def close_if_empty(session_id: str) -> bool:
    """Close the inbox when nothing is pending; False means something landed
    and the turn should read it before it ends."""
    with _lock:
        if _inboxes.get(session_id):
            return False
        _closed.add(session_id)
        return True


def close_and_take(session_id: str) -> list[Entry]:
    """Stop accepting pushes and return what was queued but never read."""
    with _lock:
        _closed.add(session_id)
        return _inboxes.pop(session_id, [])


def reopen(session_id: str) -> None:
    """Accept pushes again (a turn claimed the slot, or the slot was freed)."""
    with _lock:
        _closed.discard(session_id)


def clear(session_id: str) -> None:
    """Drop every queued entry and reopen: a fresh turn, a Stop, or /reset."""
    with _lock:
        _inboxes.pop(session_id, None)
        _closed.discard(session_id)


UNANSWERED = (
    "Your message reached this turn as it was ending, so it was not answered. "
    "Send a follow-up to get a reply."
)


def close_and_announce(session_id: str) -> list[str]:
    """Close the chat turn's inbox for good and hand on what it accepted but
    never read: agent reports get a wake turn (server/agents/wake.py), the
    same as a push the closed inbox refuses, and a message the user sent is
    announced as unanswered, on the stream and in the notifications, instead
    of going quiet on the composer's promise. Returns the SSE frames to send
    before the turn's [DONE]. Synchronous on purpose: nothing may reach the
    inbox between this and the end of the turn."""
    import logging

    log = logging.getLogger("whisper-studio")
    left = close_and_take(session_id)
    reports = [e.payload for e in left if e.kind == AGENT_REPORT and e.payload]
    if reports:
        try:
            from server.agents import wake

            how = wake.deliver_unread(session_id, reports)
            log.info("Unread agent reports at turn end (session %s): %s", session_id, how)
        except Exception as e:  # noqa: BLE001 - the reports are rows in the chat already
            log.warning("Unread agent reports not handed on (session %s): %s", session_id, e)
    if not any(e.kind == USER for e in left):
        return []
    log.info("Mid-turn message left unanswered at turn end (session %s)", session_id)
    try:
        from server.notifications import record_notification

        record_notification(
            session_id=session_id,
            source="chat",
            title="Message not answered",
            message=UNANSWERED,
            status="warning",
        )
    except Exception as e:  # noqa: BLE001 - the stream frame still says it
        log.debug("unanswered-message notification skipped: %s", e)
    from server.utils import ndjson_dumps

    return [f"data: {ndjson_dumps({'status': UNANSWERED})}\n\n"]
