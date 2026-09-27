"""Reminders reach the model even when the loop carries on after an assistant turn.

inject_reminder only appends to a user-role tail. When the loop decides an
apparent end of turn is not the end (a late mid-turn message, a pause_turn),
the tail is the assistant's own turn at the next round start, and the
final-round notice, the agent's report template, and the deadline and budget
reminders were silently dropped there. A continued answer is also kept
visually apart from the one before it.
"""

import asyncio
import json

from server.chat.engine import midturn_inbox
from server.chat.engine.events import RoundResult, TextDelta, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn


def _text(messages) -> str:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(str(b.get("text", "")) for b in c if isinstance(b, dict))
    return "\n".join(out)


class _Scripted:
    provider = "test"

    def __init__(self, stop_reasons=(), on_round=None):
        self.stop_reasons = list(stop_reasons)
        self.on_round = on_round
        self.calls: list[list[dict]] = []

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls.append([dict(m) for m in messages])
        if self.on_round:
            await self.on_round(round_num)
        stop = self.stop_reasons[round_num] if round_num < len(self.stop_reasons) else "end_turn"
        yield TextDelta(text=f"answer {round_num}")
        yield RoundResult(
            stop_reason=stop,
            content=[{"type": "text", "text": f"answer {round_num}"}],
            usage=Usage(),
        )


def _ctx(sid, adapter, policy, **kw) -> TurnContext:
    return TurnContext(
        cost_source="chat",
        session_id=sid,
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": "summarize the findings"}],
        adapter=adapter,
        policy=policy,
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
        **kw,
    )


def _run(ctx) -> list[dict]:
    async def go():
        return [c async for c in run_turn(ctx)]

    frames = []
    for chunk in asyncio.run(go()):
        if chunk.startswith("data: {"):
            frames.append(json.loads(chunk[len("data: ") :]))
    return frames


def test_final_round_notice_survives_a_late_message_continuation():
    sid = "tail-final-round"
    midturn_inbox.clear(sid)

    async def push_once(round_num):
        if round_num == 0:
            midturn_inbox.push(sid, "also list the open questions")

    adapter = _Scripted(on_round=push_once)
    _run(
        _ctx(
            sid,
            adapter,
            TurnPolicy(max_rounds=2, completion_gate=False),
            midturn_inbox=True,
            final_round_hint="REPORT TEMPLATE",
        )
    )

    assert len(adapter.calls) == 2
    last = adapter.calls[1]
    assert last[-1]["role"] == "user"
    sent = _text(last)
    assert "also list the open questions" in sent
    assert "This is the final tool round" in sent
    assert "REPORT TEMPLATE" in sent
    midturn_inbox.clear(sid)


def test_deadline_reminder_survives_a_pause_turn():
    async def slow(round_num):
        if round_num == 0:
            await asyncio.sleep(0.12)

    adapter = _Scripted(stop_reasons=["pause_turn"], on_round=slow)
    _run(
        _ctx(
            "tail-deadline",
            adapter,
            TurnPolicy(max_rounds=5, completion_gate=False, deadline_seconds=0.1),
        )
    )

    assert len(adapter.calls) == 2
    resumed = adapter.calls[1]
    assert resumed[-1]["role"] == "user"
    assert "Time is up for this turn" in _text(resumed)
    # The paused assistant content is still there to continue from.
    assert any(m["role"] == "assistant" and "answer 0" in _text([m]) for m in resumed)


def test_a_continued_answer_starts_apart_from_the_one_before():
    sid = "tail-separator"
    midturn_inbox.clear(sid)

    async def push_once(round_num):
        if round_num == 0:
            midturn_inbox.push(sid, "and in French?")

    frames = _run(
        _ctx(
            sid,
            _Scripted(on_round=push_once),
            TurnPolicy(max_rounds=10, completion_gate=False),
            midturn_inbox=True,
        )
    )
    text = "".join(f["text"] for f in frames if "text" in f)
    assert "answer 0\n\nanswer 1" in text
    midturn_inbox.clear(sid)


def test_a_message_sent_during_a_paused_round_reaches_the_resumed_one():
    sid = "tail-pause-drain"
    midturn_inbox.clear(sid)

    async def push_once(round_num):
        if round_num == 0:
            midturn_inbox.push(sid, "use the 2025 figures")

    adapter = _Scripted(stop_reasons=["pause_turn"], on_round=push_once)
    _run(
        _ctx(
            sid,
            adapter,
            TurnPolicy(max_rounds=5, completion_gate=False),
            midturn_inbox=True,
        )
    )

    resumed = adapter.calls[1]
    roles = [m["role"] for m in resumed]
    assert roles[-2:] == ["assistant", "user"]
    assert "use the 2025 figures" in _text(resumed[-1:])
    assert midturn_inbox.has_pending(sid) is False
    midturn_inbox.clear(sid)
