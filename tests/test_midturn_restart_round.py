"""A message the user sends while the model is still thinking restarts the round.

The model has not started its answer yet (no text, no tool call), so instead
of finishing a reply the message changes and then answering again, the runner
stops that round, closes the provider stream, and runs the next round with the
message in it: one answer that takes the message into account. A round whose
answer has started runs to its end (tests/test_midturn_late_message.py).

Bare TurnContext + a scripted adapter (no thread reads ahead of the runner),
the pattern from tests/test_midturn_late_message.py.
"""

import asyncio
import json

from server.chat.engine import midturn_inbox
from server.chat.engine.events import (
    RoundResult,
    TextDelta,
    ThinkingDelta,
    ThinkingStart,
    ToolCallStart,
    Usage,
)
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn

NOTE = "Dana and Miles are from my team"


class ThinkingAdapter:
    """Thinks first, then answers. On round ``pushes_on_round`` the user's
    message lands at ``push_at``: 'thinking' (before any answer) or 'tool'
    (right after a tool call started). Records whether each round's stream
    was closed."""

    provider = "test"

    def __init__(
        self,
        session_id: str,
        push_at: str = "thinking",
        pushes_on_round: int = 0,
        kind: str = midturn_inbox.USER,
    ):
        self.session_id = session_id
        self.push_at = push_at
        self.pushes_on_round = pushes_on_round
        self.kind = kind
        self.calls: list[list[dict]] = []
        self.closed: list[int] = []
        self.finished: list[int] = []

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls.append([dict(m) for m in messages])
        try:
            yield ThinkingStart()
            yield ThinkingDelta(text="drafting for all three")
            push_now = round_num == self.pushes_on_round
            if push_now and self.push_at == "thinking":
                midturn_inbox.push(self.session_id, NOTE, kind=self.kind)
            yield ThinkingDelta(text="still drafting")
            if push_now and self.push_at == "tool":
                yield ToolCallStart(id="t1", name="memory_list")
                midturn_inbox.push(self.session_id, NOTE, kind=self.kind)
            text = f"draft {round_num}"
            yield TextDelta(text=text)
            yield RoundResult(
                stop_reason="end_turn", content=[{"type": "text", "text": text}], usage=Usage()
            )
            self.finished.append(round_num)
        finally:
            self.closed.append(round_num)


def _ctx(session_id: str, adapter, max_rounds: int = 10) -> TurnContext:
    return TurnContext(
        cost_source="chat",
        session_id=session_id,
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": "write a recognition email"}],
        adapter=adapter,
        policy=TurnPolicy(max_rounds=max_rounds, completion_gate=False),
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
        midturn_inbox=True,
    )


def _frames(ctx) -> list:
    async def go():
        return "".join([c async for c in run_turn(ctx)])

    out = asyncio.run(go())
    return [
        json.loads(line[6:])
        for line in out.splitlines()
        if line.startswith("data: ") and line[6:] != "[DONE]"
    ]


def _text(messages: list) -> str:
    return json.dumps(messages)


def test_a_message_while_thinking_restarts_the_round():
    sid = "restart-1"
    midturn_inbox.clear(sid)
    adapter = ThinkingAdapter(sid)
    frames = _frames(_ctx(sid, adapter))
    keys = [next(iter(f)) for f in frames]

    # Round 0 stopped before its answer, and its stream was closed then.
    assert adapter.finished == [1]
    assert 0 in adapter.closed
    # The client hears it in order: drop this round, the message is read,
    # then the only answer.
    dropped, read = keys.index("round_discarded"), keys.index("midturn_read")
    assert dropped < read
    answer = "".join(f.get("text", "") for f in frames)
    assert answer.strip() == "draft 1"
    # Round 1 asked with the message in it, and nothing of round 0 in history.
    assert len(adapter.calls) == 2
    assert NOTE in _text(adapter.calls[1])
    assert "draft 0" not in _text(adapter.calls[1])
    assert "drafting for all three" not in _text(adapter.calls[1])
    assert midturn_inbox.has_pending(sid) is False


def test_an_answer_that_started_with_a_tool_call_runs_to_its_end():
    sid = "restart-2"
    midturn_inbox.clear(sid)

    class ToolThenStop(ThinkingAdapter):
        async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
            if round_num == 0:
                async for ev in super().stream_round(
                    messages, tools, core_count, round_num, is_last_round
                ):
                    if isinstance(ev, RoundResult):
                        # End the round on its text: no tool really runs.
                        ev = RoundResult(stop_reason="end_turn", content=ev.content, usage=Usage())
                    yield ev
                return
            async for ev in super().stream_round(
                messages, tools, core_count, round_num, is_last_round
            ):
                yield ev

    adapter = ToolThenStop(sid, push_at="tool")
    frames = _frames(_ctx(sid, adapter))
    assert "round_discarded" not in [next(iter(f)) for f in frames]
    assert 0 in adapter.finished


def test_agent_reports_never_restart_a_round():
    sid = "restart-3"
    midturn_inbox.clear(sid)
    adapter = ThinkingAdapter(sid, kind=midturn_inbox.AGENT_REPORT)
    frames = _frames(_ctx(sid, adapter))
    assert "round_discarded" not in [next(iter(f)) for f in frames]
    assert 0 in adapter.finished


def test_the_last_round_is_never_stopped():
    # No round would be left to answer the message: the round finishes and
    # the message is announced as not answered, as before.
    sid = "restart-4"
    midturn_inbox.clear(sid)
    adapter = ThinkingAdapter(sid)
    frames = _frames(_ctx(sid, adapter, max_rounds=1))
    assert "round_discarded" not in [next(iter(f)) for f in frames]
    assert adapter.finished == [0]
    midturn_inbox.clear(sid)
