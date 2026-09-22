"""One multiplexed SSE channel replaces one stream per open session.

The UI used to open /api/sessions/{id}/events once per live session. Browsers
cap concurrent connections per origin (about 6 on HTTP/1.1), so those streams
plus the chat stream exhausted the budget and left uploads and a second
session's first request queued in the browser until a running turn finished.
These pin the contract that makes one connection able to serve every session:
the bus fans out to a wildcard subscriber tagged by session id, the frame
mapping is shared with the per-session channel, and the literal route wins the
path match against /api/sessions/{session_id}.
"""

import asyncio

from server.agents.event_bus import AgentEventBus
from server.infrastructure.sessions import router as sessions_router
from server.infrastructure.sessions_routes import _session_event_frame


def test_wildcard_subscriber_receives_every_session_tagged():
    """One queue serves all sessions, and each event says which it belongs to."""

    async def main():
        bus = AgentEventBus()
        q = bus.subscribe_all()
        bus.publish("sess-a", {"type": "cron_event", "cronEvent": {"n": 1}})
        bus.publish("sess-b", {"type": "task_event", "taskEvent": {"n": 2}})
        first = await asyncio.wait_for(q.get(), timeout=1)
        second = await asyncio.wait_for(q.get(), timeout=1)
        return first, second

    (sid_a, ev_a), (sid_b, ev_b) = asyncio.run(main())
    assert sid_a == "sess-a" and ev_a["cronEvent"] == {"n": 1}
    assert sid_b == "sess-b" and ev_b["taskEvent"] == {"n": 2}


def test_per_session_subscriber_still_gets_the_bare_event():
    """Adding the wildcard must not change what existing subscribers receive."""

    async def main():
        bus = AgentEventBus()
        scoped = bus.subscribe("sess-a")
        everything = bus.subscribe_all()
        bus.publish("sess-a", {"type": "memory_event", "memoryEvent": {"writes": 1}})
        return await asyncio.wait_for(scoped.get(), timeout=1), await asyncio.wait_for(
            everything.get(), timeout=1
        )

    scoped_ev, wildcard_ev = asyncio.run(main())
    # Per-session subscribers get the event itself; wildcard gets it tagged.
    assert scoped_ev == {"type": "memory_event", "memoryEvent": {"writes": 1}}
    assert wildcard_ev == ("sess-a", {"type": "memory_event", "memoryEvent": {"writes": 1}})


def test_wildcard_receives_sessions_with_no_scoped_subscriber():
    """The old channel only delivered to sessions someone had subscribed to."""

    async def main():
        bus = AgentEventBus()
        q = bus.subscribe_all()
        bus.publish("nobody-listening", {"type": "cron_event", "cronEvent": {}})
        return await asyncio.wait_for(q.get(), timeout=1)

    sid, _ = asyncio.run(main())
    assert sid == "nobody-listening"


def test_unsubscribe_all_stops_delivery():
    async def main():
        bus = AgentEventBus()
        q = bus.subscribe_all()
        bus.unsubscribe_all(q)
        bus.publish("sess-a", {"type": "cron_event", "cronEvent": {}})
        return q.empty()

    assert asyncio.run(main()) is True


def test_frame_helper_maps_background_events_and_skips_agent_progress():
    """Both channels render the same envelopes, so they cannot drift."""
    assert _session_event_frame({"type": "cron_event", "cronEvent": {"a": 1}}) == {
        "cron_event": {"a": 1}
    }
    assert _session_event_frame({"type": "memory_event", "memoryEvent": {"a": 1}}) == {
        "memory_event": {"a": 1}
    }
    assert _session_event_frame({"type": "task_event", "taskEvent": {"a": 1}}) == {
        "task_event": {"a": 1}
    }
    # cron_progress is re-enveloped as team_progress for the existing card.
    assert _session_event_frame({"type": "cron_progress", "event": {"a": 1}}) == {
        "team_progress": {"a": 1}
    }
    assert _session_event_frame({"type": "ci_result", "ciResult": {"a": 1}}) == {
        "ci_result": {"a": 1}
    }
    assert _session_event_frame({"type": "session_message", "sessionMessage": {"a": 1}}) == {
        "session_message": {"a": 1}
    }
    # Agent runtime progress rides the chat SSE while a turn is in flight and
    # must stay skipped here so rows are not rendered twice.
    assert _session_event_frame({"type": "agent_progress", "event": {}}) is None
    assert _session_event_frame({"type": "workflow_event"}) is None


def test_agent_reports_and_answers_reach_an_idle_session():
    """Nothing else delivers these live.

    The chat SSE drainer only exists while a turn is running, so a report that
    lands with no live turn (a cancelled team, a background or resumed agent)
    and the wake turn's answer to it were published to the bus and then
    dropped here, surfacing only on the next hydrate.
    """
    assert _session_event_frame({"type": "agent_report", "agentReport": {"a": 1}}) == {
        "agent_report": {"a": 1}
    }
    assert _session_event_frame({"type": "agent_answer", "agentAnswer": {"a": 1}}) == {
        "agent_answer": {"a": 1}
    }


def test_missing_payload_key_yields_an_empty_envelope_not_a_crash():
    assert _session_event_frame({"type": "cron_event"}) == {"cron_event": {}}


def test_multiplexed_route_is_declared_before_the_session_id_route():
    """/api/sessions/events must not be swallowed by /api/sessions/{session_id}.

    FastAPI matches in declaration order, so the literal path has to come
    first or the multiplexed stream resolves to a session lookup for a
    session literally named "events".
    """
    paths = [getattr(r, "path", "") for r in sessions_router.routes]
    assert "/api/sessions/events" in paths, "multiplexed events route is missing"
    assert paths.index("/api/sessions/events") < paths.index("/api/sessions/{session_id}")
