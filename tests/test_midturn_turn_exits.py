"""Every exit that ends a chat turn hands on what its inbox accepted.

A message the user sends mid-turn is acknowledged ("answer it when this turn
finishes"), and agent reports that land during a turn are pushed into it. The
turn can still end before another round reads them: the round cap reached
after a tool round, a round that fails, the cost cap. Those exits used to
leave the text in an open inbox that the next fresh turn wiped, with nothing
said. Now the turn closes the inbox at its [DONE] and hands on what is left:
a user message is announced as unanswered, and agent reports get a wake turn.
A pause for approval is the exception, since its continuation reads them.
"""

import asyncio

import pytest

from server.agents import wake
from server.chat.engine import midturn_inbox
from server.chat.engine.events import RoundError, RoundResult, TextDelta, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn

REPORT = {
    "team_id": "t1",
    "team_name": "research",
    "agents": [{"name": "a", "agent_id": "abc", "result": "FINDINGS: it is a card network."}],
}


class _Scripted:
    """One scripted round per call; ``push`` runs while the round streams,
    after the round's drain, which is the window this is about."""

    provider = "test"

    def __init__(self, push, *, tool_call=False, fail=False):
        self.push = push
        self.tool_call = tool_call
        self.fail = fail
        self.calls = 0

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls += 1
        self.push()
        if self.fail:
            yield RoundError(message="the model server went away")
            return
        yield TextDelta(text="working on it")
        if self.tool_call:
            content = [
                {"type": "text", "text": "working on it"},
                {"type": "tool_use", "id": f"tu{round_num}", "name": "echo", "input": {}},
            ]
            yield RoundResult(stop_reason="tool_use", content=content, usage=Usage())
        else:
            content = [{"type": "text", "text": "working on it"}]
            yield RoundResult(stop_reason="end_turn", content=content, usage=Usage())


@pytest.fixture
def notes(monkeypatch):
    got: list = []
    monkeypatch.setattr("server.notifications.record_notification", lambda **kw: got.append(kw))
    return got


@pytest.fixture
def wakes(monkeypatch):
    got: list = []

    def _maybe_wake(session_id, payload, **kw):
        got.append((session_id, payload))
        return "scheduled"

    monkeypatch.setattr(wake, "maybe_wake", _maybe_wake)
    return got


def _tool_pipeline(monkeypatch, *, pending=False):
    import server.tool_executor as TE

    async def fake_batch(tool_uses, **kw):
        return list(tool_uses)

    async def fake_process(states, budget_fn, **kw):
        results = [{"type": "tool_result", "tool_use_id": t["id"], "content": "ok"} for t in states]
        return (results, [], pending, False)

    monkeypatch.setattr(TE, "execute_tool_batch", fake_batch)
    monkeypatch.setattr(TE, "process_tool_results", fake_process)


def _run(sid, adapter, max_rounds) -> str:
    ctx = TurnContext(
        cost_source="chat",
        session_id=sid,
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": "build the app"}],
        adapter=adapter,
        policy=TurnPolicy(max_rounds=max_rounds, completion_gate=False),
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
        midturn_inbox=True,
    )

    async def go():
        return "".join([c async for c in run_turn(ctx)])

    return asyncio.run(go())


def _user_push(sid):
    return lambda: midturn_inbox.push(sid, "also add a login page")


def test_the_round_cap_after_a_tool_round_announces_the_unread_message(monkeypatch, notes):
    sid = "exit-cap"
    midturn_inbox.clear(sid)
    _tool_pipeline(monkeypatch)
    out = _run(sid, _Scripted(_user_push(sid), tool_call=True), max_rounds=1)
    assert "Reached maximum tool rounds" in out
    assert "was not answered" in out
    assert out.index("was not answered") < out.index("[DONE]")
    assert [n["title"] for n in notes] == ["Message not answered"]
    # Closed and empty: a later message starts the next turn instead.
    assert midturn_inbox.push(sid, "and this?") is False
    assert midturn_inbox.has_pending(sid) is False
    midturn_inbox.clear(sid)


def test_a_failed_round_announces_the_message_it_never_read(notes):
    sid = "exit-error"
    midturn_inbox.clear(sid)
    out = _run(sid, _Scripted(_user_push(sid), fail=True), max_rounds=5)
    assert "the model server went away" in out
    assert "was not answered" in out
    assert len(notes) == 1
    assert midturn_inbox.push(sid, "and this?") is False
    midturn_inbox.clear(sid)


def test_unread_agent_reports_get_a_wake_and_no_user_notice(notes, wakes):
    """Reports are not something the user said: no 'your message' notice,
    and they are answered by a wake turn like a push the inbox refused."""
    sid = "exit-reports"
    midturn_inbox.clear(sid)

    def _push_report():
        midturn_inbox.push(
            sid, wake.reports_text(REPORT), kind=midturn_inbox.AGENT_REPORT, payload=REPORT
        )

    out = _run(sid, _Scripted(_push_report), max_rounds=1)
    assert "was not answered" not in out and notes == []
    assert [(s, p["team_name"]) for s, p in wakes] == [(sid, "research")]
    assert midturn_inbox.has_pending(sid) is False
    midturn_inbox.clear(sid)


def test_a_quiet_turn_hands_nothing_on(notes, wakes):
    sid = "exit-quiet"
    midturn_inbox.clear(sid)
    out = _run(sid, _Scripted(lambda: None), max_rounds=3)
    assert "was not answered" not in out
    assert notes == [] and wakes == []
    midturn_inbox.clear(sid)


def test_a_turn_paused_for_approval_keeps_the_message_for_its_continuation(
    monkeypatch, notes, wakes
):
    sid = "exit-paused"
    midturn_inbox.clear(sid)
    _tool_pipeline(monkeypatch, pending=True)
    out = _run(sid, _Scripted(_user_push(sid), tool_call=True), max_rounds=5)
    assert "was not answered" not in out and notes == []
    assert [e.text for e in midturn_inbox.drain(sid)] == ["also add a login page"]
    assert midturn_inbox.push(sid, "still open") is True
    midturn_inbox.clear(sid)


# ── A turn that ends before its round loop runs ──────────────────────────────


def _route_turn_with_a_queued_message(monkeypatch, sid, body, *, hold=None):
    """Start a chat turn through the real route and queue a same-session
    message before its round loop runs, then let it end. ``hold`` returns
    (entered, gate) events around a step that blocks the turn; without it the
    message is queued right after the claim, before the turn's setup has
    run at all. Returns (the second send's reply, the first turn's frames)."""
    from server.chat import routes, stream_slot
    from tests.local_chat_stack import FakeRequest, install_local_stack

    install_local_stack(monkeypatch)
    stream_slot.active_streams.pop(sid, None)
    midturn_inbox.clear(sid)
    entered, gate = hold(monkeypatch) if hold else (None, None)

    async def send_second():
        second = await routes.chat_endpoint(
            FakeRequest({"question": "are you there?", "session_id": sid, "history": []})
        )
        return second.body.decode()

    async def go():
        resp = await routes.chat_endpoint(FakeRequest({**body, "session_id": sid, "history": []}))
        reply = None if hold else await send_second()
        frames: list[str] = []

        async def consume():
            async for chunk in resp.body_iterator:
                frames.append(chunk if isinstance(chunk, str) else chunk.decode())

        consumer = asyncio.create_task(consume())
        if hold:
            await asyncio.wait_for(entered.wait(), 5)
            reply = await send_second()
            gate.set()
        await asyncio.wait_for(consumer, 5)
        return reply, frames

    try:
        return asyncio.run(go())
    finally:
        stream_slot.active_streams.pop(sid, None)


def _assert_announced(reply, frames, notes, sid):
    assert '"queued_into_running_turn":true' in reply.replace(" ", "")
    status = [f for f in frames if midturn_inbox.UNANSWERED in f]
    assert status and frames.index(status[0]) < frames.index("data: [DONE]\n\n")
    assert [n["title"] for n in notes if n["session_id"] == sid] == ["Message not answered"]
    assert not midturn_inbox.has_pending(sid)


def test_a_failed_model_load_announces_the_message_queued_during_it(monkeypatch, notes):
    from server.local import serving
    from tests.local_chat_stack import LOCAL_KEY

    def hold(monkeypatch):
        entered, gate = asyncio.Event(), asyncio.Event()

        async def _serve_turn(key, n_ctx=None, *, executor=None):
            entered.set()
            await gate.wait()
            raise RuntimeError("llama-server failed to start")

        monkeypatch.setattr(serving, "serve_turn", _serve_turn)
        return entered, gate

    sid = "exit-load-failed"
    reply, frames = _route_turn_with_a_queued_message(
        monkeypatch, sid, {"question": "hey!", "model": LOCAL_KEY}, hold=hold
    )
    assert any("llama-server failed to start" in f for f in frames)
    _assert_announced(reply, frames, notes, sid)


def test_a_failed_turn_setup_announces_the_message_queued_during_it(monkeypatch, notes):
    from server.chat import routes
    from tests.local_chat_stack import LOCAL_KEY

    def _latch(*a, **kw):
        raise RuntimeError("config exploded")

    monkeypatch.setattr(routes, "latch_session", _latch)
    sid = "exit-setup-failed"
    reply, frames = _route_turn_with_a_queued_message(
        monkeypatch, sid, {"question": "hey!", "model": LOCAL_KEY}
    )
    assert any("Failed to start the turn" in f for f in frames)
    _assert_announced(reply, frames, notes, sid)


def test_a_refused_turn_announces_the_message_queued_during_it(monkeypatch, notes):
    sid = "exit-refused"
    reply, frames = _route_turn_with_a_queued_message(
        monkeypatch, sid, {"question": "hey!", "model": "no-such-model"}
    )
    assert any("UNKNOWN_MODEL" in f for f in frames)
    _assert_announced(reply, frames, notes, sid)
