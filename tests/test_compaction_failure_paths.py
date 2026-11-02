"""Compaction failures stop visibly without deleting prior conversation history."""

import asyncio
import copy
import json

import pytest

from server.chat import compaction
from server.chat.engine.events import RoundResult, TextDelta, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn
from server.infrastructure.errors import PromptTooLongError


def _history():
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"original message {index}"}
        for index in range(13)
    ]
    history[9]["content"] = [
        {"type": "tool_use", "id": "old-call", "name": "probe", "input": {"nested": [1, {"x": 2}]}}
    ]
    history[10]["content"] = [
        {
            "type": "tool_result",
            "tool_use_id": "old-call",
            "content": [{"type": "text", "text": "Keep result"}],
        }
    ]
    history[12]["content"] = [
        {"type": "text", "text": "Keep attached context"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "eA=="}},
    ]
    return history


class _Adapter:
    provider = "test"

    def __init__(self, action):
        self.action = action
        self.requests = []

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.requests.append(copy.deepcopy(messages))
        if self.action == "overflow":
            raise PromptTooLongError("prompt is too long")
        if self.action == "tool":
            yield RoundResult(
                stop_reason="tool_use",
                content=[{"type": "tool_use", "id": "call", "name": "probe", "input": {}}],
                usage=Usage(input_tokens=10, output_tokens=2),
            )
        else:
            yield TextDelta(text="Partial answer")
            yield RoundResult(
                stop_reason="max_tokens",
                content=[{"type": "text", "text": "Partial answer"}],
                usage=Usage(input_tokens=10, output_tokens=2),
            )


def _run(adapter, messages, *, session_id="compaction-failure"):
    async def run():
        context = TurnContext(
            session_id=session_id,
            model_key="haiku",
            model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0",
            messages=messages,
            adapter=adapter,
            policy=TurnPolicy(max_rounds=20, completion_gate=False),
            loop=asyncio.get_running_loop(),
            executor=None,
            cost_source="chat",
            memory_hooks=lambda history: None,
            tool_exec_model_id="",
            tool_catalog=lambda: (
                [{"name": "probe", "input_schema": {"type": "object", "properties": {}}}],
                None,
            ),
        )
        return [frame async for frame in run_turn(context)]

    return asyncio.run(run())


def _error_frames(frames):
    return [
        json.loads(frame.removeprefix("data: ").strip())
        for frame in frames
        if frame.startswith("data: {") and '"error"' in frame
    ]


def _assert_failed_once(frames):
    errors = _error_frames(frames)
    assert len(errors) == 1
    assert "compaction" in errors[0]["error"].lower()
    assert "unchanged" in errors[0]["error"].lower()
    assert errors[0]["error_code"] == "COMPACTION_FAILED"
    payloads = [json.loads(frame[6:]) for frame in frames if frame.startswith("data: {")]
    assert not any("compaction" in payload or "round_discarded" in payload for payload in payloads)
    assert frames.count("data: [DONE]\n\n") == 1
    assert frames[-1] == "data: [DONE]\n\n"


@pytest.mark.parametrize("action", ["overflow", "tool", "output-limit"])
def test_each_engine_compaction_failure_keeps_prior_history(monkeypatch, action):
    import server.tool_executor as tool_executor
    from server.chat.engine import activity_labels

    messages = _history()
    original = copy.deepcopy(messages)
    compaction_inputs = []
    dispatched = []

    async def fail(history, *args, **kwargs):
        compaction_inputs.append(copy.deepcopy(history))
        raise compaction.CompactionError()

    async def route(name, arguments, **kwargs):
        dispatched.append(name)
        return "tool result preserved", []

    monkeypatch.setattr(compaction, "compact_messages_with_claude", fail)
    monkeypatch.setattr(compaction, "thresholds_for", lambda *args, **kwargs: (1, 2))
    monkeypatch.setattr(tool_executor, "route_tool", route)
    monkeypatch.setattr(activity_labels, "start_label_task", lambda *args, **kwargs: None)
    adapter = _Adapter(action)
    frames = _run(adapter, messages)

    _assert_failed_once(frames)
    assert len(adapter.requests) == 1
    assert len(compaction_inputs) == 1
    assert compaction_inputs[0][: len(original)] == original
    assert messages[: len(original)] == original
    if action == "tool":
        assert dispatched == ["probe"]
        assert messages[-1]["content"][0]["content"] == "tool result preserved"
    else:
        assert dispatched == []
    if action == "output-limit":
        assert "Partial answer" in "".join(frames)


def test_successful_reactive_compaction_is_attempted_only_once_per_overflow(monkeypatch):
    original = _history()
    messages = copy.deepcopy(original)
    seen = []
    replacement = [{"role": "user", "content": "A real summary"}]

    async def summarize(history, *args, **kwargs):
        seen.append(copy.deepcopy(history))
        return copy.deepcopy(replacement)

    monkeypatch.setattr(compaction, "compact_messages_with_claude", summarize)
    adapter = _Adapter("overflow")
    frames = _run(adapter, messages)
    assert len(seen) == 1
    assert seen[0] == original
    assert len(adapter.requests) == 2
    assert adapter.requests[1] == replacement
    assert messages == original
    assert len(_error_frames(frames)) == 1
    assert frames.count("data: [DONE]\n\n") == 1
    assert "synthesizing a final answer" not in "".join(frames)


def test_nonshrinking_reactive_compaction_does_not_retry_the_provider(monkeypatch):
    async def unchanged(history, *args, **kwargs):
        return copy.deepcopy(history)

    monkeypatch.setattr(compaction, "compact_messages_with_claude", unchanged)
    messages = _history()
    original = copy.deepcopy(messages)
    adapter = _Adapter("overflow")
    frames = _run(adapter, messages)
    assert len(adapter.requests) == 1
    assert messages == original
    assert _error_frames(frames)
    assert frames.count("data: [DONE]\n\n") == 1


def test_failed_initial_compaction_releases_the_busy_slot(monkeypatch):
    from server.chat import routes, stream_slot
    from tests.golden_harness import FakeBedrockClient, run_chat_turn

    async def fail(*args, **kwargs):
        raise compaction.CompactionError()

    monkeypatch.setattr(routes, "compact_messages_with_claude", fail)
    monkeypatch.setattr(routes, "thresholds_for", lambda *args, **kwargs: (1, 2))
    body = {"session_id": "setup-compaction-failure", "history": _history(), "question": "next"}
    original = copy.deepcopy(body)
    client = FakeBedrockClient([])
    lines = run_chat_turn(monkeypatch, client, body)
    assert client.requests == []
    assert body == original
    assert any('"error"' in line and "compaction" in line.lower() for line in lines)
    assert lines.count("[DONE]") == 1
    assert body["session_id"] not in stream_slot.active_streams
