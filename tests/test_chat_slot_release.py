"""The session busy slot is released on every exit of every turn.

/api/chat claims a per-session slot before any setup runs, and a new message
that finds the slot live is queued into the running turn instead of starting
one. So a slot that outlives its turn swallows every later message in the
session: the local (on-device) path used to return before the only release,
and a setup that raised or a client that left during setup leaked it too.
These tests drive the real route, and for the local case the real local
bridge, LocalAdapter and llama-server stream parser, with only the HTTP
server itself stubbed.
"""

import asyncio
import json
import time

import pytest

from server.agents import wake
from server.chat import routes, stream_slot
from server.chat.engine import midturn_inbox
from tests.local_chat_stack import LOCAL_KEY, FakeRequest, client, install_local_stack, post


@pytest.fixture
def local_stack(monkeypatch):
    return install_local_stack(monkeypatch)


@pytest.fixture(autouse=True)
def _clean_slots():
    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()
    yield
    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()


def test_two_consecutive_local_turns_both_stream_a_reply(local_stack):
    sid = "slot-local"
    midturn_inbox.drain(sid)
    http = client()

    first = post(http, sid, "hey!")
    assert first.headers["content-type"].startswith("text/event-stream")
    assert "Hi there!" in first.text
    # The finished turn gave its slot back, and the local server its busy mark.
    assert sid not in stream_slot.active_streams
    # So agent reports landing now start a wake turn instead of being parked
    # in the inbox of a turn that no longer exists.
    assert wake.turn_is_live(sid) is False
    assert local_stack.turns["started"] == local_stack.turns["ended"] == 1

    second = post(http, sid, "and a second question")
    # A real turn, not {"queued_into_running_turn": true} into a dead inbox.
    assert second.headers["content-type"].startswith("text/event-stream")
    assert "Second answer." in second.text
    assert midturn_inbox.drain(sid) == []
    # The model actually received the second message.
    assert len(local_stack.requests) == 2
    assert "and a second question" in json.dumps(local_stack.requests[1]["messages"])
    assert sid not in stream_slot.active_streams


def test_stop_during_a_local_reply_frees_the_slot_and_the_model_server(local_stack):
    """Stop closes the stream mid-answer: the turn's own cleanup releases the
    local server, and the slot is free before anything else can run."""
    sid = "slot-local-stop"

    async def go():
        resp = await routes.chat_endpoint(
            FakeRequest({"question": "hey!", "session_id": sid, "history": [], "model": LOCAL_KEY})
        )
        it = resp.body_iterator
        async for chunk in it:
            if "Hi there!" in chunk:
                break
        assert stream_slot.is_live(sid)
        await it.aclose()
        return sid in stream_slot.active_streams, dict(local_stack.turns)

    held, turns = asyncio.run(go())
    assert held is False
    assert turns["started"] == turns["ended"] == 1


def test_local_turn_heartbeats_its_slot_while_it_runs(local_stack, monkeypatch):
    """A long local turn must stay live, or a message typed while it works
    would start a second turn on the same session and model server."""
    sid = "slot-local-beat"
    refreshed: list[float] = []
    real_beat = stream_slot.beat

    def _spy(session_id, token):
        real_beat(session_id, token)
        if session_id == sid:
            refreshed.append(stream_slot.heartbeats[session_id] - token)

    monkeypatch.setattr(stream_slot, "beat", _spy)
    post(client(), sid, "hey!")
    # The local stream refreshed the heartbeat past the claim time.
    assert refreshed and max(refreshed) > 0


def test_setup_failure_frees_the_slot_for_the_retry(local_stack, monkeypatch):
    sid = "slot-setup-fail"
    midturn_inbox.drain(sid)
    import server.local.route as local_route

    real_local = local_route.local_chat_response
    calls = {"n": 0}

    def _local(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("setup exploded")
        return real_local(**kw)

    monkeypatch.setattr(local_route, "local_chat_response", _local)
    http = client()

    first = post(http, sid, "one")
    assert "Failed to start the turn: setup exploded" in first.text
    assert sid not in stream_slot.active_streams

    retry = post(http, sid, "two")
    assert retry.headers["content-type"].startswith("text/event-stream")
    assert "Hi there!" in retry.text
    assert midturn_inbox.drain(sid) == []
    assert calls["n"] == 2


def test_client_leaving_during_setup_frees_the_slot(monkeypatch):
    """Stop pressed while the status line still says "preparing": the setup
    is cancelled and the session accepts the next message at once."""
    sid = "slot-left-during-setup"
    state = {"recall_started": False, "recall_cancelled": False}

    async def _stuck_recall(*a, **kw):
        state["recall_started"] = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["recall_cancelled"] = True
            raise
        return "", 0

    import server.infrastructure.feature_flags as ff
    import server.memory.recall as recall

    real_enabled = ff.is_enabled
    monkeypatch.setattr(ff, "is_enabled", lambda name: name == "auto_memory" or real_enabled(name))
    monkeypatch.setattr(recall, "recall_memory_context", _stuck_recall)
    monkeypatch.setattr(routes, "get_workspace_path", lambda: None)

    async def go():
        resp = await routes.chat_endpoint(
            FakeRequest({"question": "hello", "session_id": sid, "history": []})
        )
        it = resp.body_iterator
        assert "preparing" in await it.__anext__()
        while not state["recall_started"]:
            await asyncio.sleep(0)
        assert stream_slot.is_live(sid)
        await it.aclose()
        held = sid in stream_slot.active_streams
        await asyncio.sleep(0)
        return held

    assert asyncio.run(go()) is False
    assert state["recall_cancelled"] is True


def test_release_never_evicts_a_turn_that_reclaimed_the_slot():
    sid = "slot-reclaimed"
    old = time.monotonic() - stream_slot.STALE_AFTER_S - 5
    new = time.monotonic()
    stream_slot.claim(sid, new, fresh=True)
    midturn_inbox.push(sid, "for the new turn")
    # The abandoned turn finally unwinds (as a Stop): it touches nothing of
    # the turn that took the session over.
    stream_slot.release(sid, old, stopped=True)
    stream_slot.beat(sid, old)
    assert stream_slot.active_streams[sid] == new
    assert stream_slot.heartbeats[sid] == new
    assert midturn_inbox.has_pending(sid) is True
    stream_slot.release(sid, new, stopped=False)
    assert sid not in stream_slot.active_streams and sid not in stream_slot.heartbeats
    midturn_inbox.clear(sid)
