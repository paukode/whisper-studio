"""A mid-turn message that lands AFTER the round's drain must still be answered.

server/chat/routes.py accepts a message sent while a turn runs (the composer
clears and promises it will be taken into account), and runner.py drains the
inbox at the top of each round. A message that arrives after the last drain
therefore used to be accepted and then dropped when the loop ended. The
runner now spends one more round on it, or says plainly that it was not
answered when there is no round left to spend. The completion gate that runs
when that round ends still reads the turn's own prompt as what was asked.

Bare TurnContext + a scripted adapter, the pattern from
tests/test_engine_agent_extensions.py: no HTTP route, no real Bedrock.
"""

import asyncio
from types import SimpleNamespace

from server.chat.engine import midturn_inbox
from server.chat.engine.events import RoundResult, TextDelta, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn


class LateMessageAdapter:
    """Answers with text every round, and pushes a user message into the
    inbox DURING the first round: after that round's drain, before the loop
    decides the turn is over. That is the race the fix is about."""

    provider = "test"

    def __init__(
        self, session_id: str, pushes_on_round: int = 0, text: str = "wait, also save it as a png"
    ):
        self.session_id = session_id
        self.pushes_on_round = pushes_on_round
        self.text = text
        self.calls: list[list[dict]] = []

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls.append([dict(m) for m in messages])
        if round_num == self.pushes_on_round:
            midturn_inbox.push(self.session_id, self.text)
        text = f"answer {round_num}"
        yield TextDelta(text=text)
        yield RoundResult(
            stop_reason="end_turn", content=[{"type": "text", "text": text}], usage=Usage()
        )


def _ctx(
    session_id: str,
    adapter,
    max_rounds: int,
    prompt: str = "draw the flow",
    completion_gate: bool = False,
) -> TurnContext:
    return TurnContext(
        cost_source="chat",
        session_id=session_id,
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": prompt}],
        adapter=adapter,
        policy=TurnPolicy(max_rounds=max_rounds, completion_gate=completion_gate),
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
        midturn_inbox=True,
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
    midturn_inbox.clear(sid)
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


def test_no_round_left_says_it_was_not_answered():
    sid = "late-2"
    midturn_inbox.clear(sid)
    adapter = LateMessageAdapter(sid)
    out = _drain_stream(_ctx(sid, adapter, max_rounds=1))

    # One round was all there was, so the turn ends, but it says so rather
    # than going quiet on the promise the composer made. The text is in the
    # chat (and in the next turn's history); it is never re-injected into a
    # later turn as a mid-turn message.
    assert len(adapter.calls) == 1
    assert "was not answered" in out
    # The turn is ending: it takes nothing more.
    assert midturn_inbox.push(sid, "and this?") is False
    midturn_inbox.clear(sid)


def test_quiet_turn_is_untouched():
    sid = "late-3"
    midturn_inbox.clear(sid)

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
    assert "was not answered" not in out


def test_the_completion_gate_still_reads_the_turns_first_prompt(monkeypatch):
    # The round that answers a late message ends the turn again and the gate
    # runs on it. What the turn owes is still what its prompt asked for, so
    # the pdf is asked for once instead of hiding behind the late message.
    from server import hooks
    from server.goals import gate
    from server.goals.requested_files import REQUEST_MARKER

    async def no_stop(*a, **k):
        return SimpleNamespace(blocked=False, reason="")

    monkeypatch.setattr(hooks, "check_stop_hooks", no_stop)
    monkeypatch.setattr(gate, "_flag_on", lambda name, default=True: name == "requested_file_check")

    sid = "late-4"
    midturn_inbox.clear(sid)
    adapter = LateMessageAdapter(sid, text="use the 2025 figures")
    _drain_stream(
        _ctx(sid, adapter, max_rounds=10, prompt="export the flow as a pdf", completion_gate=True)
    )

    # Round 1 answered the late message, the gate held the turn for the file,
    # and round 2 is the one that was asked for it.
    assert len(adapter.calls) == 3
    nudge = adapter.calls[2][-1]
    assert nudge["role"] == "user"
    assert nudge["content"].startswith(f"[completion gate] {REQUEST_MARKER}")
    assert "export the flow as a pdf" in nudge["content"]
    assert midturn_inbox.has_pending(sid) is False
