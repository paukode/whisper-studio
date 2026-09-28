"""Reports wake the parent: with no live turn a synthesis turn answers and its
reply lands as an agent_answer row; with a live turn the reports are folded
into it through the mid-turn inbox; the switch and the cost budget gate it."""

import asyncio
import contextlib
import json
import time
from types import SimpleNamespace

import pytest

from server.agents import wake
from server.chat import routes, stream_slot
from server.chat.engine import midturn_inbox

# The real readers, before the fixture below stubs them for every other test.
_REAL_HISTORY_FOR = wake._history_for
_REAL_MODEL_KEY_FOR = wake._model_key_for
_REAL_AGENT_MODEL_IDS = wake._agent_model_ids

LOCAL_ID = "local:gemma-4-12b-it-qat-q4_0"


def _normalized_config(mode: str) -> dict:
    """load_config's normalized shape: flat chat_models ids plus meta. A
    downloaded on-device model (the user's Gemma) is never in config
    chat_models: only the local registry lists it (server/chat/infra.py)."""
    return {
        "wake_parent_on_reports": True,
        "model_mode": mode,
        "default_chat_model": "local_gemma" if mode == "local" else "sonnet5.0",
        "chat_models": {"sonnet5.0": "us.anthropic.sonnet-5-0"},
        "chat_model_meta": {
            "sonnet5.0": {"id": "us.anthropic.sonnet-5-0", "label": "Sonnet 5.0"},
        },
    }


REPORT = {
    "team_id": "t1",
    "team_name": "research",
    "reason": "the turn ended first",
    "agents": [
        {
            "name": "a",
            "agent_id": "abc",
            "agent_type": "general",
            "result": "FINDINGS: the company is a payments processor in Krakow.",
            "status": "stopped",
            "stop_reason": "cancelled",
            "turns_used": 12,
        }
    ],
}


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(wake, "load_config", lambda: {"wake_parent_on_reports": True})
    monkeypatch.setattr(wake, "record_notification", lambda **kw: None)
    monkeypatch.setattr(
        wake, "_history_for", lambda sid: ("who might this company be?", "Let me research.")
    )
    monkeypatch.setattr(wake, "_agent_model_ids", lambda payload: [])
    monkeypatch.setattr(wake, "_model_key_for", lambda model_ids: "sonnet5.0")
    monkeypatch.setattr("server.costs.budget.check_budget", lambda session_id: None)
    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()
    wake._tasks.clear()
    wake._answering.clear()
    wake._late.clear()
    midturn_inbox.clear("s1")
    yield
    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()
    wake._tasks.clear()
    wake._answering.clear()
    wake._late.clear()
    midturn_inbox.clear("s1")


def _fake_headless(captured: dict, answer: str):
    async def _run(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        yield {"type": "text", "text": answer[:10]}
        yield {"type": "text", "text": answer[10:]}
        yield {"type": "done", "status": "completed", "session_id": kwargs.get("session_id")}

    return _run


def test_no_live_turn_starts_a_synthesis_turn_and_persists_the_answer(monkeypatch):
    captured: dict = {}
    emitted: list = []
    monkeypatch.setattr(
        "server.exec.headless.run_headless_turn",
        _fake_headless(captured, "It is most likely Stripe."),
    )
    monkeypatch.setattr(wake, "emit_session_event", lambda sid, **kw: emitted.append((sid, kw)))

    async def _go():
        how = wake.maybe_wake("s1", REPORT)
        assert how == "scheduled"
        await wake._tasks["s1"]
        return how

    asyncio.run(_go())
    assert "who might this company be?" in captured["prompt"]
    assert "FINDINGS: the company is a payments processor" in captured["prompt"]
    assert "Runtime wake, not typed by the user" in captured["prompt"]
    kw = captured["kwargs"]
    assert kw["ephemeral"] is True and kw["session_id"] == "s1"
    assert kw["max_rounds"] == wake.WAKE_MAX_ROUNDS and kw["model_key"] == "sonnet5.0"
    assert len(emitted) == 1
    sid, ev = emitted[0]
    assert sid == "s1" and ev["role"] == "agent_answer" and ev["payload_key"] == "agentAnswer"
    assert ev["payload"]["text"] == "It is most likely Stripe."
    assert ev["payload"]["team_name"] == "research"
    assert "s1" not in wake._tasks


def test_live_turn_gets_the_reports_through_the_midturn_inbox(monkeypatch):
    called = {"n": 0}

    async def _never(prompt, **kwargs):
        called["n"] += 1
        yield {"type": "done", "status": "completed"}

    monkeypatch.setattr("server.exec.headless.run_headless_turn", _never)
    stream_slot.active_streams["s1"] = time.monotonic()
    assert wake.maybe_wake("s1", REPORT) == "live"
    queued = midturn_inbox.drain("s1")
    assert len(queued) == 1 and "FINDINGS: the company is a payments processor" in queued[0].text
    # Delivered as agent output, never as something the user typed.
    assert queued[0].kind == midturn_inbox.AGENT_REPORT
    assert called["n"] == 0


def test_switch_off_skips_the_wake(monkeypatch):
    monkeypatch.setattr(wake, "load_config", lambda: {"wake_parent_on_reports": False})
    assert wake.maybe_wake("s1", REPORT) == "skipped:disabled"
    assert midturn_inbox.drain("s1") == []


def test_budget_exhausted_notifies_instead_of_answering(monkeypatch):
    notes: list = []
    emitted: list = []
    monkeypatch.setattr(wake, "record_notification", lambda **kw: notes.append(kw))
    monkeypatch.setattr(wake, "emit_session_event", lambda sid, **kw: emitted.append(kw))
    monkeypatch.setattr(
        "server.costs.budget.check_budget",
        lambda session_id: SimpleNamespace(
            message="cap reached", kind="session", limit=1, current=1.2
        ),
    )
    ran = {"n": 0}

    async def _never(prompt, **kwargs):
        ran["n"] += 1
        yield {"type": "done", "status": "completed"}

    monkeypatch.setattr("server.exec.headless.run_headless_turn", _never)

    async def _go():
        assert wake.maybe_wake("s1", REPORT) == "scheduled"
        await wake._tasks["s1"]

    asyncio.run(_go())
    assert ran["n"] == 0 and emitted == []
    assert notes and "not answered" in notes[0]["title"]


async def _settled(sid: str) -> None:
    """Wait for every wake of ``sid``, follow-ups included, to finish."""
    for _ in range(200):
        task = wake._tasks.get(sid)
        if task is None:
            return
        await task


def test_reports_landing_during_a_wake_get_a_follow_up_wake(monkeypatch):
    """A wake turn is one model call that reads nothing sent mid-call. Reports
    that land meanwhile are answered by the next wake, never parked in the
    chat inbox where only a later, unrelated chat turn would find them."""
    gate = asyncio.Event()
    prompts: list[str] = []
    answers: list[str] = []

    async def _slow(prompt, **kwargs):
        prompts.append(prompt)
        if len(prompts) == 1:
            await gate.wait()
        yield {"type": "text", "text": f"answer {len(prompts)}"}
        yield {"type": "done", "status": "completed"}

    monkeypatch.setattr("server.exec.headless.run_headless_turn", _slow)
    monkeypatch.setattr(
        wake, "emit_session_event", lambda sid, **kw: answers.append(kw["payload"]["text"])
    )
    late = {
        **REPORT,
        "team_name": "second",
        "agents": [{**REPORT["agents"][0], "result": "LATE: it also runs a card network."}],
    }

    async def _go():
        assert wake.maybe_wake("s1", REPORT) == "scheduled"
        first = wake._tasks["s1"]
        await asyncio.sleep(0.05)
        assert wake.maybe_wake("s1", late) == "queued"
        assert midturn_inbox.has_pending("s1") is False
        gate.set()
        await first
        await _settled("s1")

    asyncio.run(_go())
    assert len(prompts) == 2
    assert "LATE: it also runs a card network." not in prompts[0]
    assert "LATE: it also runs a card network." in prompts[1]
    assert answers == ["answer 1", "answer 2"]
    assert midturn_inbox.has_pending("s1") is False
    assert "s1" not in wake._tasks and "s1" not in wake._late


def test_reports_from_several_late_batches_share_one_follow_up(monkeypatch):
    gate = asyncio.Event()
    prompts: list[str] = []

    async def _slow(prompt, **kwargs):
        prompts.append(prompt)
        if len(prompts) == 1:
            await gate.wait()
        yield {"type": "text", "text": "ok"}
        yield {"type": "done", "status": "completed"}

    monkeypatch.setattr("server.exec.headless.run_headless_turn", _slow)
    monkeypatch.setattr(wake, "emit_session_event", lambda sid, **kw: None)
    batches = [
        {**REPORT, "team_name": name, "agents": [{**REPORT["agents"][0], "result": body}]}
        for name, body in (("b", "BODY-B"), ("c", "BODY-C"))
    ]

    async def _go():
        wake.maybe_wake("s1", REPORT)
        first = wake._tasks["s1"]
        await asyncio.sleep(0.05)
        for b in batches:
            assert wake.maybe_wake("s1", b) == "queued"
        gate.set()
        await first
        await _settled("s1")

    asyncio.run(_go())
    assert len(prompts) == 2
    assert "BODY-B" in prompts[1] and "BODY-C" in prompts[1]


def test_a_follow_up_wake_builds_on_the_previous_wake_answer(monkeypatch):
    history = [
        {"role": "user", "content": "who might this company be?"},
        {"role": "agent_report", "content": "", "agentReport": REPORT},
        {"role": "agent_answer", "content": "", "agentAnswer": {"text": "It is Stripe."}},
    ]

    class _Conn:
        def execute(self, *a):
            return self

        def fetchone(self):
            return {"chat_history": json.dumps(history)}

    monkeypatch.setattr(
        "server.infrastructure.sessions._get_conn", lambda: contextlib.nullcontext(_Conn())
    )
    last_user, last_assistant = _REAL_HISTORY_FOR("s1")
    assert last_user == "who might this company be?"
    assert last_assistant == "It is Stripe."


def test_a_follow_up_wake_is_given_the_answer_the_previous_wake_just_gave(monkeypatch):
    """The real persist path: the first wake's agent_answer row is written
    through the executor, after the follow-up wake has already read the
    stored history. The follow-up still builds on that answer."""
    from server.infrastructure import sessions
    from server.tasks import events

    sid = "wake-follow-up-real"
    sessions._append_message_sync(sid, {"role": "user", "content": "who might this company be?"})
    monkeypatch.setattr(events, "_server_loop", None)
    monkeypatch.setattr(wake, "_history_for", _REAL_HISTORY_FOR)
    gate = asyncio.Event()
    prompts: list[str] = []

    async def _slow(prompt, **kwargs):
        prompts.append(prompt)
        if len(prompts) == 1:
            await gate.wait()
            yield {"type": "text", "text": "It is most likely Stripe."}
        else:
            yield {"type": "text", "text": "The late report adds its card network."}
        yield {"type": "done", "status": "completed"}

    monkeypatch.setattr("server.exec.headless.run_headless_turn", _slow)
    late = {
        **REPORT,
        "team_name": "second",
        "agents": [{**REPORT["agents"][0], "result": "LATE: it also runs a card network."}],
    }

    def _answers() -> list[str]:
        with sessions._get_conn() as conn:
            row = conn.execute("SELECT chat_history FROM sessions WHERE id = ?", (sid,)).fetchone()
        rows = json.loads(row["chat_history"])
        return [r["agentAnswer"]["text"] for r in rows if r.get("role") == "agent_answer"]

    async def _go():
        assert wake.maybe_wake(sid, REPORT) == "scheduled"
        first = wake._tasks[sid]
        await asyncio.sleep(0.05)
        assert wake.maybe_wake(sid, late) == "queued"
        gate.set()
        await first
        await _settled(sid)
        for _ in range(100):
            if len(_answers()) == 2:
                break
            await asyncio.sleep(0.02)

    asyncio.run(_go())
    assert len(prompts) == 2
    assert "It is most likely Stripe." in prompts[1]
    assert "LATE: it also runs a card network." in prompts[1]
    assert _answers() == ["It is most likely Stripe.", "The late report adds its card network."]


def _run_wake(monkeypatch, headless):
    """Schedule a wake with no live turn; return (headless calls, rows, notes)."""
    calls: list = []
    rows: list = []
    notes: list = []

    async def _spy(prompt, **kwargs):
        calls.append(kwargs)
        async for ev in headless(prompt, **kwargs):
            yield ev

    monkeypatch.setattr("server.exec.headless.run_headless_turn", _spy)
    monkeypatch.setattr(wake, "emit_session_event", lambda sid, **kw: rows.append(kw))
    monkeypatch.setattr(wake, "record_notification", lambda **kw: notes.append(kw))

    async def _go():
        assert wake.maybe_wake("s1", REPORT) == "scheduled"
        await _settled("s1")

    asyncio.run(_go())
    return calls, rows, notes


def test_local_mode_never_wakes_on_a_cloud_model(monkeypatch):
    monkeypatch.setattr(wake, "load_config", lambda: _normalized_config("local"))
    # No journal names a model: the headless default would be a cloud model.
    monkeypatch.setattr(wake, "_model_key_for", lambda model_ids: None)
    calls, rows, notes = _run_wake(monkeypatch, _fake_headless({}, "cloud answer"))
    assert calls == [] and rows == []
    assert len(notes) == 1 and "Local mode" in notes[0]["message"]
    assert "next message" in notes[0]["message"]


def test_reports_from_on_device_agents_are_not_sent_to_a_cloud_wake(monkeypatch):
    """Hybrid mode, agents ran on a downloaded Gemma that config chat_models
    does not list: the journaled id alone says it is on-device."""
    monkeypatch.setattr(wake, "load_config", lambda: _normalized_config("hybrid"))
    monkeypatch.setattr(wake, "_agent_model_ids", _REAL_AGENT_MODEL_IDS)
    monkeypatch.setattr(wake, "_model_key_for", _REAL_MODEL_KEY_FOR)
    monkeypatch.setattr(
        "server.agents.journal.load",
        lambda agent_id, session_id=None: {"meta": {"model": LOCAL_ID}},
    )
    calls, rows, notes = _run_wake(monkeypatch, _fake_headless({}, "cloud answer"))
    assert calls == [] and rows == []
    assert len(notes) == 1 and "on-device model" in notes[0]["message"]


def test_one_on_device_agent_in_a_mixed_team_keeps_the_wake_off_the_cloud(monkeypatch):
    monkeypatch.setattr(wake, "load_config", lambda: _normalized_config("cloud"))
    monkeypatch.setattr(wake, "_agent_model_ids", _REAL_AGENT_MODEL_IDS)
    journals = {"abc": "us.anthropic.sonnet-5-0", "def": LOCAL_ID}
    monkeypatch.setattr(
        "server.agents.journal.load",
        lambda agent_id, session_id=None: {"meta": {"model": journals[agent_id]}},
    )
    team = {**REPORT, "agents": [REPORT["agents"][0], {**REPORT["agents"][0], "agent_id": "def"}]}
    calls: list = []

    async def _spy(prompt, **kwargs):
        calls.append(kwargs)
        yield {"type": "done", "status": "completed"}

    monkeypatch.setattr("server.exec.headless.run_headless_turn", _spy)

    async def _go():
        assert wake.maybe_wake("s1", team) == "scheduled"
        await _settled("s1")

    asyncio.run(_go())
    assert calls == []


def test_the_wake_runs_on_the_model_the_agents_ran_on(monkeypatch):
    monkeypatch.setattr(
        "server.chat.infra._get_chat_models",
        lambda: {"sonnet5.0": "us.anthropic.sonnet-5-0", "local_gemma": LOCAL_ID},
    )
    assert _REAL_MODEL_KEY_FOR(["us.anthropic.sonnet-5-0"]) == "sonnet5.0"
    assert _REAL_MODEL_KEY_FOR([]) is None


def test_a_failed_wake_is_a_notification_not_an_assistant_answer(monkeypatch):
    async def _refusing(prompt, **kwargs):
        yield {"type": "error", "message": "Unknown chat model key: 'gone'"}
        yield {"type": "done", "status": "failed"}

    calls, rows, notes = _run_wake(monkeypatch, _refusing)
    assert len(calls) == 1
    assert rows == []
    assert len(notes) == 1 and notes[0]["status"] == "warning"
    assert "Unknown chat model key" in notes[0]["message"]


def test_a_cloud_session_still_gets_its_wake_answer(monkeypatch):
    monkeypatch.setattr(wake, "load_config", lambda: _normalized_config("cloud"))
    calls, rows, notes = _run_wake(monkeypatch, _fake_headless({}, "It is most likely Stripe."))
    assert [c["model_key"] for c in calls] == ["sonnet5.0"]
    assert [r["payload"]["text"] for r in rows] == ["It is most likely Stripe."]
    assert notes and notes[0]["title"].startswith("Answer ready")


def test_agent_answer_rows_are_the_assistants_own_turn():
    from server.infrastructure.sessions import visible_chat_history

    row = {
        "role": "agent_answer",
        "content": "",
        "timestamp": "t",
        "agentAnswer": {"text": "It is Stripe."},
    }
    out = visible_chat_history([{"role": "user", "content": "who?"}, row])
    assert out[1] == {"role": "assistant", "content": "It is Stripe.", "timestamp": "t"}


LATE = {
    **REPORT,
    "team_name": "second",
    "agents": [{**REPORT["agents"][0], "result": "LATE: it also runs a card network."}],
}


def _blocking_headless(state: dict):
    """A wake whose model call never returns until it is cancelled."""

    async def _run(prompt, **kwargs):
        state["runs"] = state.get("runs", 0) + 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        yield {"type": "text", "text": "an answer nobody asked for any more"}

    return _run


def test_a_message_sent_during_a_wake_supersedes_it(monkeypatch):
    """The wake held no chat slot, so a message typed while it ran started a
    second turn beside it, and both answered the reports."""
    from tests.local_chat_stack import LOCAL_KEY, FakeRequest, install_local_stack

    stack = install_local_stack(monkeypatch)
    monkeypatch.setattr(wake, "load_config", lambda: _normalized_config("cloud"))
    rows: list = []
    monkeypatch.setattr(wake, "emit_session_event", lambda sid, **kw: rows.append(kw))
    state: dict = {}
    monkeypatch.setattr("server.exec.headless.run_headless_turn", _blocking_headless(state))
    sid = "wake-superseded"

    async def _go():
        assert wake.maybe_wake(sid, REPORT) == "scheduled"
        wake_task = wake._tasks[sid]
        while not state.get("runs"):
            await asyncio.sleep(0)
        resp = await routes.chat_endpoint(
            FakeRequest(
                {"question": "any news?", "session_id": sid, "history": [], "model": LOCAL_KEY}
            )
        )
        body = "".join([c async for c in resp.body_iterator])
        # Bounded: a wake left running would otherwise hang the test.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(wake_task, 5)
        return body, wake_task

    body, wake_task = asyncio.run(_go())
    assert "Hi there!" in body and len(stack.requests) == 1
    assert state.get("cancelled") is True and wake_task.cancelled()
    assert rows == [] and sid not in wake._tasks


def test_the_turn_that_supersedes_a_wake_reads_the_reports_it_answers(monkeypatch):
    """The reports are an agent_report row in the chat, and the client ships
    that row (src/hooks/chatStream/history.ts), so the superseding turn's
    model request carries them."""
    from tests.local_chat_stack import LOCAL_KEY, FakeRequest, install_local_stack

    stack = install_local_stack(monkeypatch)
    monkeypatch.setattr(wake, "load_config", lambda: _normalized_config("cloud"))
    monkeypatch.setattr(wake, "emit_session_event", lambda sid, **kw: None)
    state: dict = {}
    monkeypatch.setattr("server.exec.headless.run_headless_turn", _blocking_headless(state))
    sid = "wake-superseded-reads"
    history = [
        {"role": "user", "content": "who might this company be?"},
        {"role": "assistant", "content": "Let me research."},
        {"role": "agent_report", "content": "", "agentReport": REPORT},
    ]

    async def _go():
        assert wake.maybe_wake(sid, REPORT) == "scheduled"
        wake_task = wake._tasks[sid]
        while not state.get("runs"):
            await asyncio.sleep(0)
        resp = await routes.chat_endpoint(
            FakeRequest(
                {"question": "any news?", "session_id": sid, "history": history, "model": LOCAL_KEY}
            )
        )
        "".join([c async for c in resp.body_iterator])
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(wake_task, 5)
        return wake_task

    wake_task = asyncio.run(_go())
    assert wake_task.cancelled()
    assert "FINDINGS: the company is a payments processor" in json.dumps(stack.requests)


def test_a_turn_refused_at_model_resolution_leaves_the_wake_answering(monkeypatch):
    from tests.local_chat_stack import FakeRequest, install_local_stack

    install_local_stack(monkeypatch)
    monkeypatch.setattr(wake, "load_config", lambda: _normalized_config("cloud"))
    monkeypatch.setattr(wake, "emit_session_event", lambda sid, **kw: None)
    state: dict = {}
    monkeypatch.setattr("server.exec.headless.run_headless_turn", _blocking_headless(state))
    sid = "wake-refused-turn"

    async def _go():
        assert wake.maybe_wake(sid, REPORT) == "scheduled"
        wake_task = wake._tasks[sid]
        while not state.get("runs"):
            await asyncio.sleep(0)
        resp = await routes.chat_endpoint(
            FakeRequest(
                {"question": "hi", "session_id": sid, "history": [], "model": "no-such-model"}
            )
        )
        body = "".join([c async for c in resp.body_iterator])
        await asyncio.sleep(0.05)
        alive = not wake_task.done() and wake._tasks.get(sid) is wake_task
        wake_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wake_task
        return body, alive

    body, alive = asyncio.run(_go())
    assert "UNKNOWN_MODEL" in body
    assert alive, "a refused turn cancelled the wake answering the reports"


@pytest.mark.parametrize("fresh", [True, False])
def test_the_chat_turn_takes_the_reports_of_the_wake_it_supersedes(monkeypatch, fresh):
    """A fresh turn has the reports as agent_report rows in its history; an
    approval continuation runs on its stashed conversation, so its inbox gets
    them. Either way no follow-up wake runs beside the chat turn."""
    state: dict = {}
    monkeypatch.setattr("server.exec.headless.run_headless_turn", _blocking_headless(state))

    async def _go():
        assert wake.maybe_wake("s1", REPORT) == "scheduled"
        task = wake._tasks["s1"]
        while not state.get("runs"):
            await asyncio.sleep(0)
        assert wake.maybe_wake("s1", LATE) == "queued"
        # What the route does for a turn whose model resolved.
        stream_slot.claim("s1", time.monotonic(), fresh=fresh)
        wake.yield_to_chat_turn("s1", fresh=fresh)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await asyncio.sleep(0.05)

    asyncio.run(_go())
    assert state["runs"] == 1 and state.get("cancelled") is True
    assert "s1" not in wake._tasks and "s1" not in wake._late
    entries = midturn_inbox.drain("s1")
    if fresh:
        assert entries == []
    else:
        assert [e.kind for e in entries] == [midturn_inbox.AGENT_REPORT] * 2
        assert "FINDINGS: the company" in entries[0].text
        assert "LATE: it also runs a card network." in entries[1].text
