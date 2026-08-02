"""The engine's prompt-too-long handling: reactive trim, then the salvage
round (new in the turn-engine cutover — previously the turn died with
'Conversation too long even after compaction').

A client whose invoke keeps rejecting with a too-long error exercises:
  1. history > 4 messages → trim + compact + retry (old behavior preserved)
  2. history too small to trim → ONE final no-tools salvage round that
     synthesizes an answer from the trimmed tail
  3. salvage itself rejected → the terminal error (old message preserved)
"""

import json

from tests.golden_harness import (
    FakeBedrockClient,
    msg_end,
    msg_start,
    run_chat_turn,
    text_block,
)


class RejectingThenServingClient(FakeBedrockClient):
    """Rejects the first ``rejections`` invokes with a prompt-too-long error,
    then serves scripted rounds."""

    def __init__(self, rounds, rejections=1):
        super().__init__(rounds)
        self.rejections = rejections
        self.invokes = 0

    def invoke_model_with_response_stream(self, **kw):
        self.invokes += 1
        if self.invokes <= self.rejections:
            raise Exception("ValidationException: prompt is too long: 281096 tokens > 278528")
        return super().invoke_model_with_response_stream(**kw)


def test_salvage_round_rescues_short_history(monkeypatch):
    # History is tiny (one user message), so the reactive trim path can't
    # shrink it — the engine must fall through to the salvage round and still
    # produce an answer.
    client = RejectingThenServingClient(
        [[msg_start(), *text_block("Salvaged summary."), *msg_end()]],
        rejections=1,
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "huge question"})
    joined = "\n".join(lines)
    assert "Salvaged summary." in joined
    assert "synthesizing a final answer" in joined
    assert lines[-1] == "[DONE]"
    # The salvage request must carry no tools and end with the truncation note.
    salvage_req = client.requests[-1]
    assert "tools" not in salvage_req or not salvage_req.get("tools")
    assert "truncated" in json.dumps(salvage_req["messages"][-1])


def test_salvage_failure_surfaces_terminal_error(monkeypatch):
    # Both the original round and the salvage round reject: the user gets the
    # terminal too-long error (previous behavior), not a hang.
    client = RejectingThenServingClient([], rejections=99)
    lines = run_chat_turn(monkeypatch, client, {"question": "huge question"})
    joined = "\n".join(lines)
    assert "Conversation too long even after compaction" in joined
    assert lines[-1] == "[DONE]"


def test_reactive_trim_still_retries_long_histories(monkeypatch):
    # With a long history the engine trims + compacts + retries (parity with
    # the old loop) instead of jumping straight to salvage.
    history = []
    for i in range(4):
        history.append({"role": "user", "content": f"q{i}"})
        history.append({"role": "assistant", "content": f"a{i}"})
    client = RejectingThenServingClient(
        [[msg_start(), *text_block("Recovered."), *msg_end()]],
        rejections=1,
    )
    lines = run_chat_turn(
        monkeypatch, client, {"question": "next", "history": history}
    )
    joined = "\n".join(lines)
    assert "Recovered." in joined
    assert "Compacting context (prompt too long)" in joined
    # Salvage was NOT needed.
    assert "synthesizing a final answer" not in joined
