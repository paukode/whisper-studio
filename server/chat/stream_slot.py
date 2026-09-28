"""The per-session busy slot of the chat stream.

POST /api/chat claims a session's slot before any turn setup runs, and a new
message that finds the slot live is folded into the running turn through the
mid-turn inbox (server/chat/engine/midturn_inbox.py) instead of starting a
second one. server/agents/wake.py reads it too, to decide whether agent
reports go into a running turn or get a wake turn of their own.

The slot is released in exactly one place, the outer stream's ``finally`` in
server/chat/routes.py, which every exit of every turn passes through. A slot
that outlived its turn would swallow every later message in the session, so
a stream that was abandoned without a clean disconnect is also reclaimed once
its heartbeat is older than ``STALE_AFTER_S``.
"""

import asyncio
import time

# Session id -> monotonic start time of its in-flight chat stream. The value
# doubles as the ownership token: only the turn that claimed the slot may
# refresh or release it. A dict (not a set) so an abandoned stream (one whose
# connection was suspended or closed without a clean disconnect, leaving the
# generator parked so its finally never ran) can be detected as stale and
# reclaimed; otherwise the session would stay busy until the app restarts.
active_streams: dict[str, float] = {}
# Session id -> monotonic time of last observed progress (refreshed at every
# round boundary and by the keepalive). Staleness is judged against THIS, not
# the start time in active_streams (the token must not be mutated), so a
# legitimately long multi-round turn is never wrongly reclaimed.
heartbeats: dict[str, float] = {}
# A stream silent for longer than this (seconds) is presumed abandoned and its
# slot is reclaimed on the next turn. The /reset endpoint and the client
# disconnect poll free it sooner on the common paths; this is the backstop for
# a suspended connection that never cleanly disconnects.
STALE_AFTER_S = 900.0  # above a single Bedrock read timeout (~600s)
# How often a live stream refreshes its heartbeat while it is silent.
KEEPALIVE_S = 30.0


def is_live(session_id: str, now: float | None = None) -> bool:
    """Whether a chat turn holds this session's slot and has shown progress
    within the stale window."""
    busy = active_streams.get(session_id)
    if busy is None:
        return False
    last = heartbeats.get(session_id, busy)
    return ((time.monotonic() if now is None else now) - last) < STALE_AFTER_S


def claim(session_id: str, token: float, *, fresh: bool) -> None:
    """Take the session for a chat turn.

    A fresh turn starts with an empty inbox: anything still queued there
    predates it, and the client already sent that text as part of this
    turn's history, so a leftover must never be injected again as a mid-turn
    message. An approval continuation is the same turn carrying on, so what
    was sent to it before the pause is kept.

    A wake turn answering agent reports (server/agents/wake.py) gives way to
    the chat turn once its model is resolved (wake.yield_to_chat_turn, called
    by the route), so a turn refused at model resolution leaves it running."""
    from server.chat.engine import midturn_inbox

    active_streams[session_id] = token
    heartbeats[session_id] = token
    if fresh:
        midturn_inbox.clear(session_id)
    else:
        midturn_inbox.reopen(session_id)


def beat(session_id: str, token: float) -> None:
    """Refresh the heartbeat, only while ``token`` still owns the slot: a turn
    whose slot was reclaimed as stale must not keep the new owner's alive."""
    if active_streams.get(session_id) == token:
        heartbeats[session_id] = time.monotonic()


def drop(session_id: str) -> bool:
    """Free the slot whoever holds it (/reset). True when one was held."""
    cleared = active_streams.pop(session_id, None) is not None
    heartbeats.pop(session_id, None)
    return cleared


def release(session_id: str, token: float, *, stopped: bool) -> None:
    """Free the slot ``token`` claimed. A later turn that reclaimed the session
    as stale holds a different token and is never evicted.

    ``stopped`` is a turn that did not run to its end (Stop, a closed tab):
    whatever was queued for it is dropped, so it can never surface in a later
    turn. A finished turn leaves its inbox as it is for an approval
    continuation; the next fresh turn clears it."""
    from server.chat.engine import midturn_inbox

    if active_streams.get(session_id) != token:
        return
    drop(session_id)
    if stopped:
        midturn_inbox.clear(session_id)
    else:
        midturn_inbox.reopen(session_id)


async def aclose(it) -> None:
    """Close an async iterator now instead of whenever it is garbage collected,
    so its ``finally`` (a turn's own cleanup) runs before the caller moves on."""
    close = getattr(it, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001 - already closed or exhausted
        pass


async def with_heartbeat(chunks, on_beat, interval: float = KEEPALIVE_S):
    """Forward ``chunks``, calling ``on_beat`` on every chunk and every
    ``interval`` seconds the turn itself is working without output.

    The runner heartbeats once per round, but one tool call can outlast the
    stale window on its own: a team of agents working for an hour, a long
    shell command. Judged stale, the slot refused the user's mid-turn message
    (409, the composer kept the text) or, before 2.8.1, let a second turn
    start on top of the running one.

    The pulse beats only while this wrapper waits on the turn, never while it
    is parked at ``yield`` waiting for the client to take a chunk. A client
    that stopped reading without a clean disconnect leaves the generator
    parked for good (its ``finally`` never runs), and a pulse that kept
    beating then would make the slot look live forever: every later message
    in the session would be queued into a turn that can no longer read it.
    Parked, the heartbeat ages out and the stale reclaim frees the session.
    """
    waiting_on_turn = True

    async def _pulse():
        while True:
            await asyncio.sleep(interval)
            if waiting_on_turn:
                on_beat()

    pulse = asyncio.create_task(_pulse())
    try:
        async for chunk in chunks:
            waiting_on_turn = False
            on_beat()
            yield chunk
            waiting_on_turn = True
    finally:
        pulse.cancel()
        await aclose(chunks)
