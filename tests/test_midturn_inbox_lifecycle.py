"""When the mid-turn inbox accepts text, and when what is queued is dropped.

A message queued for a turn must reach that turn or be dropped with it; it
must never surface in a later, unrelated turn as a "mid-turn" note (the model
then saw it twice, once as plain history and once framed as a steer of the new
request). And a turn that has taken its last look at the inbox must refuse
further pushes, so the text starts the next turn instead of waiting in a queue
nobody reads.
"""

import asyncio
import json
import time

import pytest

from server.chat import routes, stream_slot
from server.chat.engine import midturn_inbox
from server.chat.engine.events import RoundResult, TextDelta, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn
from tests.local_chat_stack import LOCAL_KEY, FakeRequest, client, install_local_stack, post


@pytest.fixture
def local_stack(monkeypatch):
    return install_local_stack(monkeypatch)


@pytest.fixture(autouse=True)
def _clean():
    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()
    yield
    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()


# ── Route: claim, Stop, reset ───────────────────────────────────────────────


def test_a_fresh_turn_never_reads_text_left_from_an_earlier_turn(local_stack):
    sid = "life-fresh"
    # Queued for a turn that is gone; the client already has it as a bubble,
    # so it rides in this turn's history.
    midturn_inbox.clear(sid)
    midturn_inbox.push(sid, "stranded words")
    r = post(client(), sid, "new question")
    assert "Hi there!" in r.text
    sent = json.dumps(local_stack.requests[0]["messages"])
    assert "stranded words" not in sent
    assert midturn_inbox.has_pending(sid) is False


def test_an_approval_continuation_keeps_what_was_sent_to_its_turn():
    sid = "life-continuation"
    midturn_inbox.clear(sid)
    midturn_inbox.push(sid, "also check the tests")
    stream_slot.claim(sid, time.monotonic(), fresh=False)
    assert [e.text for e in midturn_inbox.drain(sid)] == ["also check the tests"]


def test_stop_drops_what_was_queued_for_the_stopped_turn(local_stack):
    sid = "life-stop"
    midturn_inbox.clear(sid)

    async def go():
        resp = await routes.chat_endpoint(
            FakeRequest({"question": "hey!", "session_id": sid, "history": [], "model": LOCAL_KEY})
        )
        it = resp.body_iterator
        await it.__anext__()
        # The user types into the running turn, then presses Stop.
        assert midturn_inbox.push(sid, "never mind, do it differently")
        await it.aclose()

    asyncio.run(go())
    assert sid not in stream_slot.active_streams
    assert midturn_inbox.has_pending(sid) is False


def test_reset_drops_the_queue_too():
    sid = "life-reset"
    midturn_inbox.clear(sid)
    stream_slot.claim(sid, time.monotonic(), fresh=True)
    midturn_inbox.push(sid, "are you stuck?")
    r = client().post(f"/api/chat/sessions/{sid}/reset")
    assert r.json()["cleared_stream"] is True
    assert midturn_inbox.has_pending(sid) is False
    assert sid not in stream_slot.heartbeats


def test_a_turn_that_is_ending_refuses_the_push_and_the_message_starts_the_next(local_stack):
    sid = "life-ending"
    midturn_inbox.clear(sid)
    # A live slot whose turn has already taken its last look.
    stream_slot.claim(sid, time.monotonic(), fresh=True)
    assert midturn_inbox.close_if_empty(sid) is True
    http = client()

    steer = http.post("/api/chat", json={"question": "and?", "session_id": sid, "midturn": True})
    # The composer keeps the text and says nothing was sent.
    assert steer.status_code == 409

    fresh = post(http, sid, "next question")
    assert fresh.headers["content-type"].startswith("text/event-stream")
    assert "Hi there!" in fresh.text
    assert "next question" in json.dumps(local_stack.requests[-1]["messages"])


# ── Runner: the last look ──────────────────────────────────────────────────


class _Answering:
    provider = "test"

    def __init__(self):
        self.calls: list[str] = []

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        parts = []
        for m in messages:
            c = m.get("content")
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                parts.extend(str(b.get("text", "")) for b in c if isinstance(b, dict))
        self.calls.append("\n".join(parts))
        yield TextDelta(text=f"answer {round_num}")
        yield RoundResult(
            stop_reason="end_turn",
            content=[{"type": "text", "text": f"answer {round_num}"}],
            usage=Usage(),
        )


def _chat_ctx(sid, adapter, *, gate: bool, max_rounds: int = 10) -> TurnContext:
    return TurnContext(
        cost_source="chat",
        session_id=sid,
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": "draw the flow"}],
        adapter=adapter,
        policy=TurnPolicy(max_rounds=max_rounds, completion_gate=gate),
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
        midturn_inbox=True,
    )


def _run(ctx) -> str:
    async def go():
        return "".join([c async for c in run_turn(ctx)])

    return asyncio.run(go())


def test_a_message_sent_while_the_completion_gate_runs_is_answered(monkeypatch):
    """The gate awaits Stop hooks and the goal evaluator. A message that lands
    then used to be acknowledged and never read."""
    from server.goals import GateDecision

    sid = "life-gate"
    midturn_inbox.clear(sid)

    async def _gate(gctx):
        if gctx.attempt == 0 and not _gate.pushed:
            _gate.pushed = True
            midturn_inbox.push(sid, "wait, use the blue theme")
        return GateDecision(block=False)

    _gate.pushed = False

    monkeypatch.setattr("server.goals.gate.run_completion_gate", _gate)
    adapter = _Answering()
    _run(_chat_ctx(sid, adapter, gate=True))

    assert len(adapter.calls) == 2
    assert "use the blue theme" in adapter.calls[1]
    assert midturn_inbox.has_pending(sid) is False


def test_a_finished_turn_refuses_later_pushes():
    sid = "life-closed"
    midturn_inbox.clear(sid)
    _run(_chat_ctx(sid, _Answering(), gate=False))
    assert midturn_inbox.push(sid, "too late for that turn") is False
    assert midturn_inbox.has_pending(sid) is False
    midturn_inbox.clear(sid)


def test_no_round_left_says_the_message_was_not_answered():
    sid = "life-last-round"
    midturn_inbox.clear(sid)

    class _PushesDuringRound(_Answering):
        async def stream_round(self, *a, **kw):
            midturn_inbox.push(sid, "one more thing")
            async for ev in super().stream_round(*a, **kw):
                yield ev

    out = _run(_chat_ctx(sid, _PushesDuringRound(), gate=False, max_rounds=1))
    assert "was not answered" in out
    # Closed: nothing more is accepted for this turn.
    assert midturn_inbox.push(sid, "and another") is False
    midturn_inbox.clear(sid)
