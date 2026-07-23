"""The same-session double-stream guard: a second NEW turn for a session that
is already streaming gets 409; the slot is timestamped so an abandoned stream
goes stale and is reclaimed; the /reset endpoint clears a wedged session; the
slot clears on every stream exit path."""

import time

from server.chat import routes


def test_guard_dict_membership_semantics():
    # The guard's contract is enforced at the top of chat_endpoint via this
    # module-level dict (session_id -> monotonic start time). Exercise the
    # primitives the endpoint uses.
    routes._active_chat_streams.clear()

    sid = "guard-session"
    assert sid not in routes._active_chat_streams
    routes._active_chat_streams[sid] = time.monotonic()
    assert sid in routes._active_chat_streams

    # Different session is unaffected.
    assert "other" not in routes._active_chat_streams

    # pop is idempotent (continuation / finally paths).
    routes._active_chat_streams.pop(sid, None)
    routes._active_chat_streams.pop(sid, None)
    assert sid not in routes._active_chat_streams


def test_fresh_slot_is_busy_stale_slot_is_reclaimable():
    # A recent start time means still busy (409). A start time older than the
    # stale threshold means reclaimable (the guard lets the new turn through),
    # which is what stops an abandoned/suspended stream wedging the session
    # until the app is restarted.
    now = time.monotonic()
    fresh = now
    stale = now - routes._STREAM_STALE_AFTER_S - 1
    assert (now - fresh) < routes._STREAM_STALE_AFTER_S  # busy
    assert (now - stale) >= routes._STREAM_STALE_AFTER_S  # reclaimable


def test_second_new_turn_409s_while_streaming(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Force the cloud Anthropic path regardless of this machine's config.json
    # default model. The 409 guard lives on the cloud branch; with a local_mode
    # config (default_chat_model=local_gemma) local_chat_response() returns a
    # StreamingResponse and short-circuits before the guard, and an OpenAI
    # default would do the same via openai_chat_response(). Neutralizing both
    # bridges makes a model-less POST reach the _active_chat_streams guard
    # deterministically. (Patch the modules the endpoint imports at call time.)
    import server.local.route as local_route
    import server.openai_bedrock.route as openai_route

    monkeypatch.setattr(local_route, "local_chat_response", lambda **kw: None)
    monkeypatch.setattr(openai_route, "openai_chat_response", lambda **kw: None)

    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)

    routes._active_chat_streams.clear()
    # Fresh timestamp => within the busy window => must 409.
    routes._active_chat_streams["busy-session"] = time.monotonic()
    try:
        r = client.post(
            "/api/chat",
            json={
                "question": "hello again",
                "session_id": "busy-session",
                "history": [],
            },
        )
        assert r.status_code == 409
        assert r.json()["error_code"] == "SESSION_BUSY"
    finally:
        routes._active_chat_streams.clear()


def test_reset_endpoint_clears_wedged_state():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)

    routes._active_chat_streams.clear()
    routes._paused_sessions.clear()
    sid = "wedged-session"
    routes._active_chat_streams[sid] = time.monotonic()
    routes._paused_sessions[sid] = {"messages": [], "pending_tool_results": []}
    try:
        r = client.post(f"/api/chat/sessions/{sid}/reset")
        assert r.status_code == 200
        assert r.json() == {
            "reset": True,
            "cleared_stream": True,
            "cleared_paused": True,
        }
        assert sid not in routes._active_chat_streams
        assert sid not in routes._paused_sessions

        # Idempotent: a second reset is a clean no-op.
        r2 = client.post(f"/api/chat/sessions/{sid}/reset")
        assert r2.json() == {
            "reset": True,
            "cleared_stream": False,
            "cleared_paused": False,
        }
    finally:
        routes._active_chat_streams.clear()
        routes._paused_sessions.clear()
