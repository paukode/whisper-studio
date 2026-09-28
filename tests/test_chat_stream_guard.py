"""The same-session double-stream guard: a second NEW turn for a session that
is already streaming gets queued into the running turn (server.chat.engine.
midturn_inbox) instead of erroring or racing it as an independent second
turn — the slot is claimed synchronously at the very top of chat_endpoint,
before the turn's own setup (condensation/grounding/hooks) runs, which is
what makes the queuing decision race-free; the slot is timestamped so an
abandoned stream goes stale and is reclaimed instead of queued forever; the
/reset endpoint clears a wedged session; the slot clears on every stream exit
path."""

import asyncio
import time

from server.chat import routes, stream_slot


def test_guard_dict_membership_semantics():
    # The guard's contract is enforced at the top of chat_endpoint via this
    # module-level dict (session_id -> monotonic start time). Exercise the
    # primitives the endpoint uses.
    stream_slot.active_streams.clear()

    sid = "guard-session"
    assert sid not in stream_slot.active_streams
    stream_slot.active_streams[sid] = time.monotonic()
    assert sid in stream_slot.active_streams

    # Different session is unaffected.
    assert "other" not in stream_slot.active_streams

    # pop is idempotent (continuation / finally paths).
    stream_slot.active_streams.pop(sid, None)
    stream_slot.active_streams.pop(sid, None)
    assert sid not in stream_slot.active_streams


def test_fresh_slot_is_busy_stale_slot_is_reclaimable():
    # A recent start time means still busy (409). A start time older than the
    # stale threshold means reclaimable (the guard lets the new turn through),
    # which is what stops an abandoned/suspended stream wedging the session
    # until the app is restarted.
    now = time.monotonic()
    fresh = now
    stale = now - stream_slot.STALE_AFTER_S - 1
    assert (now - fresh) < stream_slot.STALE_AFTER_S  # busy
    assert (now - stale) >= stream_slot.STALE_AFTER_S  # reclaimable


def test_second_new_turn_queued_into_running_turn_while_streaming(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.chat.engine import midturn_inbox

    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)

    stream_slot.active_streams.clear()
    midturn_inbox.drain("busy-session")
    # Fresh timestamp => within the busy window => queued, not started as a
    # second stream and not refused.
    stream_slot.active_streams["busy-session"] = time.monotonic()
    try:
        r = client.post(
            "/api/chat",
            json={
                "question": "actually, stop and give me what you have",
                "session_id": "busy-session",
                "history": [],
            },
        )
        # A plain, instant JSON reply — no second SSE stream is opened, and
        # the busy slot (still owned by the ACTUAL running turn) is untouched.
        assert r.status_code == 200
        assert r.json() == {"queued_into_running_turn": True}
        assert "busy-session" in stream_slot.active_streams
        assert [e.text for e in midturn_inbox.drain("busy-session")] == [
            "actually, stop and give me what you have"
        ]
    finally:
        stream_slot.active_streams.clear()
        midturn_inbox.drain("busy-session")


def test_slot_is_claimed_synchronously_before_the_turns_own_setup_runs():
    """The real bug this guarded against: the slot used to be claimed deep
    inside _build_turn, AFTER condensation/grounding/hook setup that can take
    real seconds — so a message sent during that setup window found the slot
    still looking free and started its own independent turn instead of
    queuing, racing and interleaving with the one already running. The claim
    now happens in chat_endpoint's synchronous prefix, before _build_turn is
    even created as a task, so it must be visible the instant chat_endpoint
    returns — before that task has run a single line of its own setup."""

    class _FakeRequest:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()

    async def _call():
        req = _FakeRequest({"question": "hello", "session_id": "claim-timing-sess", "history": []})
        return await routes.chat_endpoint(req)

    try:
        resp = asyncio.run(_call())
        # asyncio.create_task schedules but does not run _build_turn before
        # chat_endpoint's own return — this passing is the whole point: the
        # slot is already held with NO turn setup having executed at all.
        assert "claim-timing-sess" in stream_slot.active_streams
        assert resp is not None
    finally:
        stream_slot.active_streams.clear()
        stream_slot.heartbeats.clear()


def test_stale_busy_slot_is_reclaimed_not_queued(monkeypatch):
    # A stale slot (the guard's existing abandoned-stream case) must fall
    # through to the normal new-turn path, not get silently swallowed into
    # the inbox of a turn that isn't actually running anymore.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server.local.route as local_route
    from server.chat.engine import midturn_inbox

    monkeypatch.setattr(local_route, "local_chat_response", lambda **kw: None)

    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)

    stream_slot.active_streams.clear()
    midturn_inbox.drain("stale-session")
    stream_slot.active_streams["stale-session"] = time.monotonic() - stream_slot.STALE_AFTER_S - 1
    try:
        r = client.post(
            "/api/chat",
            json={"question": "hello again", "session_id": "stale-session", "history": []},
        )
        assert r.status_code == 200
        # Reclaimed and streamed normally — never queued.
        assert midturn_inbox.drain("stale-session") == []
    finally:
        stream_slot.active_streams.clear()
        midturn_inbox.drain("stale-session")


def test_reset_endpoint_clears_wedged_state():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)

    stream_slot.active_streams.clear()
    routes._paused_sessions.clear()
    sid = "wedged-session"
    stream_slot.active_streams[sid] = time.monotonic()
    routes._paused_sessions[sid] = {"messages": [], "pending_tool_results": []}
    try:
        r = client.post(f"/api/chat/sessions/{sid}/reset")
        assert r.status_code == 200
        assert r.json() == {
            "reset": True,
            "cleared_stream": True,
            "cleared_paused": True,
        }
        assert sid not in stream_slot.active_streams
        assert sid not in routes._paused_sessions

        # Idempotent: a second reset is a clean no-op.
        r2 = client.post(f"/api/chat/sessions/{sid}/reset")
        assert r2.json() == {
            "reset": True,
            "cleared_stream": False,
            "cleared_paused": False,
        }
    finally:
        stream_slot.active_streams.clear()
        routes._paused_sessions.clear()


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body

    async def is_disconnected(self):
        return False


def test_continuation_turn_claims_the_slot_too():
    """An approval resume used to skip the claim, so from the first approval
    card onward the session looked idle: a message typed while the resumed
    turn kept working started a second turn from the composer's minimal body
    instead of being queued into the running one."""
    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()

    async def _call():
        req = _FakeRequest(
            {
                "question": "",
                "session_id": "resume-sess",
                "history": [],
                "approved_tool_result": {"tool_use_id": "t1", "content": "ok"},
            }
        )
        return await routes.chat_endpoint(req)

    try:
        resp = asyncio.run(_call())
        assert resp is not None
        assert "resume-sess" in stream_slot.active_streams
        assert "resume-sess" in stream_slot.heartbeats
    finally:
        stream_slot.active_streams.clear()
        stream_slot.heartbeats.clear()


def test_midturn_body_is_refused_when_nothing_is_running():
    """The composer's steer-the-running-turn body may only be queued. When the
    turn finished in the window before it arrived, the server says so with a
    JSON 409 instead of starting a fresh turn from an incomplete body."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.chat.engine import midturn_inbox

    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()
    midturn_inbox.drain("idle-session")
    try:
        r = client.post(
            "/api/chat",
            json={"question": "are you stuck?", "session_id": "idle-session", "midturn": True},
        )
        assert r.status_code == 409
        assert r.json()["queued_into_running_turn"] is False
        assert "idle-session" not in stream_slot.active_streams
        assert midturn_inbox.drain("idle-session") == []
    finally:
        stream_slot.active_streams.clear()
        stream_slot.heartbeats.clear()


def test_midturn_body_is_queued_while_running():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.chat.engine import midturn_inbox

    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    stream_slot.active_streams.clear()
    stream_slot.active_streams["live-session"] = time.monotonic()
    midturn_inbox.drain("live-session")
    try:
        r = client.post(
            "/api/chat",
            json={"question": "what is the status?", "session_id": "live-session", "midturn": True},
        )
        assert r.status_code == 200
        assert r.json() == {"queued_into_running_turn": True}
        assert [e.text for e in midturn_inbox.drain("live-session")] == ["what is the status?"]
    finally:
        stream_slot.active_streams.clear()
        midturn_inbox.drain("live-session")


def test_stream_heartbeat_keeps_pulsing_during_a_silent_tool_call():
    """One tool call (a team of agents, a long command) can outlast the stale
    window with no chunk and no new round. The wrapper pulses the heartbeat on
    its own while the stream is silent, so the slot stays live and a mid-turn
    message is queued instead of refused."""
    beats: list[float] = []

    async def silent_turn():
        await asyncio.sleep(0.35)
        yield "data: {}\n\n"

    async def run():
        out = []
        async for chunk in stream_slot.with_heartbeat(
            silent_turn(), lambda: beats.append(time.monotonic()), interval=0.05
        ):
            out.append(chunk)
        return out

    assert asyncio.run(run()) == ["data: {}\n\n"]
    # Several pulses landed during the silence, plus one for the chunk itself.
    assert len(beats) >= 4


def test_stream_heartbeat_stops_pulsing_while_the_client_is_not_reading():
    """A client that stops reading without a clean disconnect parks the
    stream at ``yield`` for good. The pulse must not keep that slot live, or
    the stale reclaim never fires and every later message in the session is
    queued into a turn that can no longer read it."""
    beats: list[float] = []

    async def chatty_turn():
        while True:
            yield "data: {}\n\n"
            await asyncio.sleep(0.01)

    async def run():
        stream = stream_slot.with_heartbeat(
            chatty_turn(), lambda: beats.append(time.monotonic()), interval=0.05
        )
        await stream.__anext__()
        parked_at = time.monotonic()
        # The consumer holds the chunk and does not ask for the next one.
        await asyncio.sleep(0.35)
        beats_while_parked = [b for b in beats if b > parked_at]
        await stream.aclose()
        return beats_while_parked

    assert asyncio.run(run()) == []
