"""A mid-turn message that lands AFTER the round's drain must still be answered.

server/chat/routes.py accepts a message sent while a turn runs (the composer
clears and promises it will be taken into account), and runner.py drains the
inbox at the top of each round. A message that arrives after the last drain
therefore used to be accepted and then dropped when the loop ended. The
runner now spends one more round on it, or says plainly that it will be
picked up next turn when there is no round left to spend.

Bare TurnContext + a scripted adapter, the pattern from
tests/test_engine_agent_extensions.py: no HTTP route, no real Bedrock.
"""

import asyncio

from server.chat.engine import midturn_inbox
from server.chat.engine.events import RoundResult, TextDelta, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn


class LateMessageAdapter:
    """Answers with text every round, and pushes a user message into the
    inbox DURING the first round: after that round's drain, before the loop
    decides the turn is over. That is the race the fix is about."""

    provider = "test"

    def __init__(self, session_id: str, pushes_on_round: int = 0):
        self.session_id = session_id
        self.pushes_on_round = pushes_on_round
        self.calls: list[list[dict]] = []

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls.append([dict(m) for m in messages])
        if round_num == self.pushes_on_round:
            midturn_inbox.push(self.session_id, "wait, also save it as a png")
        text = f"answer {round_num}"
        yield TextDelta(text=text)
        yield RoundResult(
            stop_reason="end_turn", content=[{"type": "text", "text": text}], usage=Usage()
        )


def _ctx(session_id: str, adapter, max_rounds: int) -> TurnContext:
    return TurnContext(
        session_id=session_id,
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": "draw the flow"}],
        adapter=adapter,
        policy=TurnPolicy(max_rounds=max_rounds, completion_gate=False),
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
    )


def _drain_stream(ctx) -> str:
    async def go():
        return "".join([c async for c in run_turn(ctx)])

    return asyncio.run(go())


def _messages_text(messages: list) -> str:
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.extend(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return " ".join(out)


def test_message_arriving_after_the_last_drain_gets_another_round():
    sid = "late-1"
    midturn_inbox.drain(sid)
    adapter = LateMessageAdapter(sid)
    _drain_stream(_ctx(sid, adapter, max_rounds=10))

    # Round 0 looked like the end of the turn, but a message had landed: the
    # loop ran again instead of ending.
    assert len(adapter.calls) == 2
    # And that round is the one that actually carries the user's words.
    assert "<user_message_mid_turn>" in _messages_text(adapter.calls[1])
    assert "save it as a png" in _messages_text(adapter.calls[1])
    # Nothing left stranded.
    assert midturn_inbox.has_pending(sid) is False


def test_no_round_left_says_so_and_keeps_the_message_queued():
    sid = "late-2"
    midturn_inbox.drain(sid)
    adapter = LateMessageAdapter(sid)
    out = _drain_stream(_ctx(sid, adapter, max_rounds=1))

    # One round was all there was, so the turn ends — but it says where the
    # message went rather than going quiet on a promise the composer made.
    assert len(adapter.calls) == 1
    assert "picked up on the next turn" in out
    # Still queued, so the next turn's round-zero drain answers it.
    assert midturn_inbox.has_pending(sid) is True
    assert midturn_inbox.drain(sid) == ["wait, also save it as a png"]


def test_quiet_turn_is_untouched():
    sid = "late-3"
    midturn_inbox.drain(sid)

    class QuietAdapter(LateMessageAdapter):
        async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
            self.calls.append([dict(m) for m in messages])
            yield TextDelta(text="done")
            yield RoundResult(
                stop_reason="end_turn", content=[{"type": "text", "text": "done"}], usage=Usage()
            )

    adapter = QuietAdapter(sid)
    out = _drain_stream(_ctx(sid, adapter, max_rounds=10))
    assert len(adapter.calls) == 1
    assert "picked up on the next turn" not in out
