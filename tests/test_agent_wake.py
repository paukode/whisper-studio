"""Reports wake the parent: with no live turn a synthesis turn answers and its
reply lands as an agent_answer row; with a live turn the reports are folded
into it through the mid-turn inbox; the switch and the cost budget gate it."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from server.agents import wake
from server.chat import routes
from server.chat.engine import midturn_inbox

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
    monkeypatch.setattr(wake, "_model_key_for", lambda payload: "sonnet5.0")
    monkeypatch.setattr("server.costs.budget.check_budget", lambda session_id: None)
    routes._active_chat_streams.clear()
    routes._stream_heartbeat.clear()
    wake._waking.clear()
    yield
    routes._active_chat_streams.clear()
    routes._stream_heartbeat.clear()
    wake._waking.clear()
    midturn_inbox.drain("s1")


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
    assert "s1" not in wake._waking


def test_live_turn_gets_the_reports_through_the_midturn_inbox(monkeypatch):
    called = {"n": 0}

    async def _never(prompt, **kwargs):
        called["n"] += 1
        yield {"type": "done", "status": "completed"}

    monkeypatch.setattr("server.exec.headless.run_headless_turn", _never)
    routes._active_chat_streams["s1"] = time.monotonic()
    assert wake.maybe_wake("s1", REPORT) == "live"
    queued = midturn_inbox.drain("s1")
    assert len(queued) == 1 and "FINDINGS: the company is a payments processor" in queued[0]
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


def test_second_wake_while_one_runs_is_folded_into_it(monkeypatch):
    gate = asyncio.Event()

    async def _slow(prompt, **kwargs):
        await gate.wait()
        yield {"type": "text", "text": "done"}
        yield {"type": "done", "status": "completed"}

    monkeypatch.setattr("server.exec.headless.run_headless_turn", _slow)
    monkeypatch.setattr(wake, "emit_session_event", lambda sid, **kw: None)

    async def _go():
        assert wake.maybe_wake("s1", REPORT) == "scheduled"
        await asyncio.sleep(0.05)
        assert wake.maybe_wake("s1", {**REPORT, "team_name": "second"}) == "live"
        assert any("second" in q for q in midturn_inbox.drain("s1"))
        gate.set()
        await wake._tasks["s1"]

    asyncio.run(_go())


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
