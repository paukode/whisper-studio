"""Approval cards the server showed, by id: the one record of each card.

A card is issued when a turn pauses on it (server/tool_approvals.py) and
moves pending -> running -> done. Its Yes (POST /api/approval/execute with
the card_id) takes it: only a pending card runs, so a second click, a retry
or an answered card is refused instead of running the action again, and the
action, payload and sandbox scope are the ones the server recorded when it
showed the card, never what a client posts back. The turn's answer closes
the card too (the continuation for its tool call), so a denied card cannot
run later.

Cards live in memory, like the paused turn they belong to
(server/chat/engine/pause.py): a backend restart drops them, and a card from
before it is refused with a reason the user can act on.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

CardState = Literal["pending", "running", "done"]

# A card nobody answered is dropped after a day; done cards go at once.
CARD_TTL_S = 24 * 3600

UNKNOWN = (
    "This approval card is no longer valid (the app restarted, or the session "
    "was reset), so nothing ran. Ask again to get a new card."
)
TAKEN = "This approval card was already answered, so it did not run again."


@dataclass
class Card:
    card_id: str
    session_id: str
    action: str
    payload: dict
    tool_use_id: str | None
    state: CardState = "pending"
    issued_at: float = field(default_factory=time.time)


_cards: dict[str, Card] = {}
_lock = threading.Lock()


def _prune_locked(now: float) -> None:
    for cid in [c.card_id for c in _cards.values() if now - c.issued_at > CARD_TTL_S]:
        if _cards[cid].state != "running":
            del _cards[cid]


def issue(*, session_id: str, action: str, payload: dict, tool_use_id: str | None) -> str:
    """Record a card the turn pauses on; returns its id for the frame."""
    card = Card(
        card_id=uuid.uuid4().hex,
        session_id=session_id,
        action=action,
        payload=dict(payload),
        tool_use_id=tool_use_id,
    )
    with _lock:
        _prune_locked(card.issued_at)
        _cards[card.card_id] = card
    return card.card_id


def take(card_id: str) -> Card | str:
    """Start running a card: the Card when it was pending, else the reason it
    may not run."""
    with _lock:
        card = _cards.get(card_id)
        if card is None:
            return UNKNOWN
        if card.state != "pending":
            return TAKEN
        card.state = "running"
        return card


def finish(card_id: str) -> None:
    """A card that ran is done; it never runs again."""
    with _lock:
        _cards.pop(card_id, None)


def close_answered(session_id: str, tool_use_ids: list[str]) -> None:
    """The turn resumed with an answer for these tool calls (approved or
    denied): their pending cards are done. A card still running keeps its
    record until its action returns."""
    ids = set(tool_use_ids)
    with _lock:
        for cid in [
            c.card_id
            for c in _cards.values()
            if c.session_id == session_id and c.tool_use_id in ids and c.state == "pending"
        ]:
            del _cards[cid]


def forget_session(session_id: str) -> int:
    """Drop a session's pending cards (its reset); returns how many."""
    with _lock:
        doomed = [
            c.card_id
            for c in _cards.values()
            if c.session_id == session_id and c.state == "pending"
        ]
        for cid in doomed:
            del _cards[cid]
    return len(doomed)
