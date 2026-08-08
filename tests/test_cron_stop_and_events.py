"""Cron live progress events and cooperative stop."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import cron_events


@pytest.fixture(autouse=True)
def clean_registry():
    with cron_events._lock:
        cron_events._stop_events.clear()
    yield
    with cron_events._lock:
        cron_events._stop_events.clear()


def test_stop_registry_lifecycle():
    assert cron_events.request_stop("j1") is False  # nothing running
    cron_events.open_run("j1")
    assert cron_events.stop_requested("j1") is False
    assert cron_events.request_stop("j1") is True
    assert cron_events.stop_requested("j1") is True
    cron_events.close_run("j1")
    assert cron_events.stop_requested("j1") is False


def test_emit_progress_payload_shape(monkeypatch):
    published: list = []
    from server.agents import event_bus as bus_mod

    monkeypatch.setattr(bus_mod.event_bus, "publish", lambda s, ev: published.append((s, ev)))
    cron_events.emit_progress(
        "sess-1",
        run_id="r1",
        job_name="nightly",
        phase="tool_call",
        turn=2,
        tool_name="web_search",
        tool_input_preview="{'query': 'x'}",
    )
    assert len(published) == 1
    sid, ev = published[0]
    assert sid == "sess-1"
    assert ev["type"] == "cron_progress"
    payload = ev["event"]
    # TeamProgressEvent contract: these fields make the existing fold render
    # a cron run as a solo agent card.
    assert payload["agent_id"] == "cron:r1"
    assert payload["team_id"] == "cron:r1"
    assert payload["agent_name"] == "nightly"
    assert payload["agent_type"] == "cron"
    assert payload["phase"] == "tool_call"
    assert payload["tool_name"] == "web_search"
    assert payload["turn"] == 2


def test_emit_progress_no_session_is_noop(monkeypatch):
    published: list = []
    from server.agents import event_bus as bus_mod

    monkeypatch.setattr(bus_mod.event_bus, "publish", lambda s, ev: published.append(ev))
    cron_events.emit_progress("", run_id="r", job_name="n", phase="started")
    assert published == []


@pytest.fixture
def cron_client(tmp_path, monkeypatch):
    import server.cron_scheduler as cs

    monkeypatch.setattr(cs, "CRON_PATH", str(tmp_path / "cron_jobs.json"))
    app = FastAPI()
    app.include_router(cs.router)
    return TestClient(app), cs


def test_stop_endpoint_unknown_job(cron_client):
    client, _cs = cron_client
    assert client.post("/api/cron/nope/stop").status_code == 404


def test_stop_endpoint_not_running(cron_client, tmp_path):
    client, cs = cron_client
    jobs = [{"id": "j9", "name": "idle", "prompt": "p", "schedule": {}, "enabled": True}]
    cs.save_cron_jobs(jobs)
    assert client.post("/api/cron/j9/stop").status_code == 409


def test_stop_endpoint_running_job(cron_client):
    client, cs = cron_client
    jobs = [{"id": "j5", "name": "busy", "prompt": "p", "schedule": {}, "enabled": True}]
    cs.save_cron_jobs(jobs)
    with cs._IN_PROGRESS_LOCK:
        cs._in_progress.add("j5")
    cron_events.open_run("j5")
    try:
        r = client.post("/api/cron/j5/stop")
        assert r.status_code == 200
        assert r.json() == {"stopping": True}
        assert cron_events.stop_requested("j5") is True
    finally:
        with cs._IN_PROGRESS_LOCK:
            cs._in_progress.discard("j5")


def test_session_events_forwards_cron_progress_as_team_progress():
    """The long-lived SSE forwards cron_progress inside the team_progress
    envelope (the frontend folds it with the existing team logic)."""
    import inspect

    from server.infrastructure import sessions_routes as SR

    src = inspect.getsource(SR.session_events)
    assert "cron_progress" in src
    assert "team_progress" in src


def test_chat_drainer_skips_cron_progress():
    """The in-turn drainer must skip cron_progress (double delivery). The
    drainer lives in the unified turn engine since the P2/P3 cutover."""
    import inspect

    import server.chat.engine.runner as runner

    src = inspect.getsource(runner)
    assert '"cron_progress",' in src


def test_chat_drainer_skips_session_message():
    """A cross-session message has its own delivery path (the long-lived
    session stream below) — the in-turn drainer must skip it too, or a
    message sent to a session with an active turn in flight gets mis-wrapped
    as a bogus team_progress frame instead of rendering as a chat card."""
    import inspect

    import server.chat.engine.runner as runner

    src = inspect.getsource(runner)
    assert '"session_message",' in src


def test_session_events_yields_session_message():
    """The long-lived session SSE actually forwards a queued session_message
    event as `{session_message: <payload>}` — this is the ONLY delivery path
    for a message sent to a session with no turn in flight, so this exercises
    the real generator rather than just checking the source mentions it."""
    import asyncio

    from server.agents.event_bus import event_bus
    from server.infrastructure import sessions_routes as SR

    class _FakeRequest:
        async def is_disconnected(self):
            return False

    async def _run_with_patched_bus():
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(
            {
                "type": "session_message",
                "sessionMessage": {
                    "from_session_id": "a",
                    "from_title": "Session A",
                    "content": "the news is done",
                    "timestamp": "2026-08-08T00:00:00Z",
                },
            }
        )
        orig_subscribe = event_bus.subscribe
        orig_unsubscribe = event_bus.unsubscribe
        event_bus.subscribe = lambda sid: queue
        event_bus.unsubscribe = lambda sid, q: None
        try:
            response = await SR.session_events("s1", _FakeRequest())
            chunk = await response.body_iterator.__anext__()
        finally:
            event_bus.subscribe = orig_subscribe
            event_bus.unsubscribe = orig_unsubscribe
        return chunk

    chunk = asyncio.run(_run_with_patched_bus())
    assert "session_message" in chunk
    assert "the news is done" in chunk


def test_session_events_forwards_session_message():
    """Source-level parity check, matching the style of the cron_progress
    test above: the branch and payload key exist in the route source."""
    import inspect

    from server.infrastructure import sessions_routes as SR

    src = inspect.getsource(SR.session_events)
    assert '"session_message"' in src
    assert "sessionMessage" in src


def test_stop_endpoint_reaches_deleted_but_running_job(cron_client):
    """A job deleted while its run is in flight must still be stoppable:
    the live registry is consulted before the jobs file."""
    client, cs = cron_client
    cs.save_cron_jobs([])  # job no longer on disk
    with cs._IN_PROGRESS_LOCK:
        cs._in_progress.add("ghost")
    cron_events.open_run("ghost")
    try:
        r = client.post("/api/cron/ghost/stop")
        assert r.status_code == 200
        assert r.json() == {"stopping": True}
    finally:
        with cs._IN_PROGRESS_LOCK:
            cs._in_progress.discard("ghost")
