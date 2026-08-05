"""Stop-reason handling in the unified chat turn loop.

Regression coverage for the "chat stops/pauses for no reason mid-task, no error
shown" bug. Anthropic returns ``stop_reason=pause_turn`` when it pauses a
long-running turn (adaptive thinking / long tool streams run past the stream
window). The engine used to fall through to its terminal branch and end the
turn silently. It must instead RESUBMIT the accumulated content and continue,
while a genuine ``end_turn`` still stops and the round cap still bounds a pause
storm.
"""

import json

from tests.golden_harness import (
    FakeBedrockClient,
    msg_end,
    msg_start,
    run_chat_turn,
    text_block,
)


def test_pause_turn_resubmits_and_continues(monkeypatch):
    # Round 1 pauses mid-work; round 2 finishes. The turn must span both rounds
    # with a single [DONE] at the very end.
    client = FakeBedrockClient(
        [
            [
                msg_start(),
                *text_block("Cloning the repository..."),
                *msg_end(stop_reason="pause_turn"),
            ],
            [msg_start(), *text_block("Inspection complete."), *msg_end(stop_reason="end_turn")],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "inspect the repo"})
    joined = "\n".join(lines)

    assert "Cloning the repository..." in joined
    assert "Inspection complete." in joined
    assert lines[-1] == "[DONE]"
    # A pause is NOT the end of the turn: exactly one terminal frame.
    assert lines.count("[DONE]") == 1
    # The engine resubmitted — two model rounds were invoked.
    assert len(client.requests) == 2
    # ...and the resubmit carried the paused assistant content back, so the
    # model continues from where it paused rather than starting over.
    round2_last = client.requests[1]["messages"][-1]
    assert round2_last["role"] == "assistant"
    assert any(
        b.get("type") == "text" and "Cloning the repository" in b.get("text", "")
        for b in round2_last["content"]
    )


def test_end_turn_still_terminates(monkeypatch):
    # Only ONE round is scripted; the FakeBedrockClient raises on any extra
    # invoke. If the loop wrongly kept going past end_turn this fails loudly.
    client = FakeBedrockClient(
        [
            [msg_start(), *text_block("All done."), *msg_end(stop_reason="end_turn")],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "hi"})
    assert "All done." in "\n".join(lines)
    assert lines[-1] == "[DONE]"
    assert len(client.requests) == 1  # a real end_turn stops after one round


def test_max_tokens_midwork_continues(monkeypatch):
    # Output-cap truncation mid-answer must auto-continue (pin the existing
    # behavior alongside the new pause_turn path).
    client = FakeBedrockClient(
        [
            [msg_start(), *text_block("partial answer"), *msg_end(stop_reason="max_tokens")],
            [msg_start(), *text_block(" and the rest."), *msg_end(stop_reason="end_turn")],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "write a long answer"})
    joined = "\n".join(lines)

    assert "partial answer" in joined
    assert "and the rest." in joined
    assert lines[-1] == "[DONE]"
    assert len(client.requests) == 2
    # The continuation injected the "continue where you left off" nudge.
    round2_last = client.requests[1]["messages"][-1]
    assert round2_last["role"] == "user"
    assert "Continue exactly where you left off" in json.dumps(round2_last)


def test_pause_turn_storm_is_bounded(monkeypatch):
    # A model stuck pausing every round must not spin forever: the round cap
    # bounds it. Lower CHAT_POLICY.max_rounds so the test is cheap.
    import server.chat.engine.policy as policy_mod
    from server.chat.engine.policy import TurnPolicy

    monkeypatch.setattr(policy_mod, "CHAT_POLICY", TurnPolicy(max_rounds=3))

    def pausing_round():
        return [msg_start(), *text_block("still working"), *msg_end(stop_reason="pause_turn")]

    client = FakeBedrockClient([pausing_round() for _ in range(10)])
    lines = run_chat_turn(monkeypatch, client, {"question": "go"})
    joined = "\n".join(lines)

    assert lines[-1] == "[DONE]"
    assert "(Reached maximum tool rounds)" in joined
    # Exactly max_rounds model invokes — the pause storm was bounded, not looped.
    assert len(client.requests) == 3


def test_pause_turn_without_content_does_not_hang(monkeypatch):
    # Defensive: a pause with no assistant content can't be resubmitted as a
    # well-formed turn. The engine must fall through to a normal terminal exit
    # (via the completion gate) rather than resubmit an empty assistant turn.
    client = FakeBedrockClient(
        [
            [msg_start(), *msg_end(stop_reason="pause_turn")],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "edge"})
    assert lines[-1] == "[DONE]"
    assert len(client.requests) == 1
