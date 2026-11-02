"""Provider overflow is fail-closed, never a hard-tail salvage request."""

import copy
import json

from tests.golden_harness import (
    FakeBedrockClient,
    msg_end,
    msg_start,
    run_chat_turn,
    text_block,
)


class RejectingThenServingClient(FakeBedrockClient):
    """Reject initial requests, capturing rejected histories as well."""

    def __init__(self, rounds, rejections=1):
        super().__init__(rounds)
        self.rejections = rejections
        self.invokes = 0
        self.rejected = []

    def invoke_model_with_response_stream(self, **kw):
        self.invokes += 1
        if self.invokes <= self.rejections:
            self.rejected.append(json.loads(kw["body"]))
            raise Exception("ValidationException: prompt is too long: 281096 tokens > 278528")
        return super().invoke_model_with_response_stream(**kw)


def _failed(lines):
    errors = [json.loads(line)["error"] for line in lines if line.startswith('{"error"')]
    assert len(errors) == 1
    assert "Context compaction failed" in errors[0]
    assert "history is unchanged" in errors[0]
    assert lines.count("[DONE]") == 1
    assert lines[-1] == "[DONE]"
    assert "synthesizing a final answer" not in "\n".join(lines)


def test_short_history_refuses_without_salvage(monkeypatch):
    client = RejectingThenServingClient(
        [[msg_start(), *text_block("Must not synthesize."), *msg_end()]], rejections=1
    )
    body = {"question": "huge question"}
    original = copy.deepcopy(body)
    lines = run_chat_turn(monkeypatch, client, body)
    _failed(lines)
    assert client.invokes == 1
    assert client.requests == []
    assert "Must not synthesize." not in "\n".join(lines)
    assert body == original


def test_repeated_overflow_stops_after_one_successful_compaction(monkeypatch):
    from server.chat import compaction

    seen = []

    async def summarize(messages, *args, **kwargs):
        seen.append(copy.deepcopy(messages))
        return [{"role": "user", "content": "Summary"}]

    monkeypatch.setattr(compaction, "compact_messages_with_claude", summarize)
    history = [{"role": "user", "content": "Original context " * 100}]
    client = RejectingThenServingClient([], rejections=99)
    lines = run_chat_turn(monkeypatch, client, {"question": "next", "history": history})
    _failed(lines)
    assert len(seen) == 1
    assert client.invokes == 2
    assert seen[0] == [*history, {"role": "user", "content": "next"}]
    retry = client.rejected[1]["messages"]
    assert len(retry) == 1 and retry[0]["role"] == "user"
    assert retry[0]["content"][0]["text"] == "Summary"


def test_reactive_summary_sees_complete_history_and_keeps_recent_suffix(monkeypatch):
    from server.chat import compaction

    history = []
    for i in range(8):
        history.extend(
            [
                {"role": "user", "content": f"q{i} " * 100},
                {"role": "assistant", "content": f"a{i} " * 100},
            ]
        )
    original = copy.deepcopy(history)
    seen = []

    async def summarize(messages, *args, **kwargs):
        seen.append(copy.deepcopy(messages))
        assert kwargs["trigger"] == "context-overflow"
        return [
            {"role": "user", "content": "Summary of original prefix"},
            *copy.deepcopy(messages[-9:]),
        ]

    monkeypatch.setattr(compaction, "compact_messages_with_claude", summarize)
    client = RejectingThenServingClient([[msg_start(), *text_block("Recovered."), *msg_end()]])
    lines = run_chat_turn(monkeypatch, client, {"question": "next", "history": history})
    assert "Recovered." in "\n".join(lines)
    assert len(seen) == 1
    assert seen[0] == [*history, {"role": "user", "content": "next"}]
    assert client.requests[0]["messages"][-9:] == client.rejected[0]["messages"][-9:]
    assert client.requests[0]["tools"] == client.rejected[0]["tools"]
    assert history == original
    assert lines.count("[DONE]") == 1


def test_context_failure_with_goal_reports_not_checked_without_judge(monkeypatch):
    from server.goals import store as goal_store

    def no_judge(*args, **kwargs):
        raise AssertionError("the judge ran after context failure")

    monkeypatch.setattr("server.goals.evaluator.evaluate", no_judge)
    sid = "context-failure-with-goal"
    goal_store.set_goal(sid, "summarise the huge question")
    client = RejectingThenServingClient([], rejections=1)
    lines = run_chat_turn(monkeypatch, client, {"question": "huge question", "session_id": sid})
    _failed(lines)
    evals = [json.loads(line)["goal_eval"] for line in lines if line.startswith('{"goal_eval"')]
    assert [entry["verdict"] for entry in evals] == ["not_checked"]
    assert "context compaction failed" in evals[0]["feedback"]
    assert "without completing the answer" in evals[0]["feedback"]
    assert goal_store.is_active(sid)
    assert goal_store.get_goal(sid)["state"]["last_verdict"] == "not_checked"
    assert client.invokes == 1
