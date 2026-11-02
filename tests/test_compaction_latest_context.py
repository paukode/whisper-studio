"""Later failures retain the working context produced by a successful compaction."""

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError

from server.chat import compaction
from server.chat.engine.events import RoundResult, Usage
from server.chat.engine.openai import _is_prompt_too_long
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn
from server.infrastructure.errors import PromptTooLongError
from tests.test_compaction_failure_paths import _assert_failed_once, _history
from tests.test_engine_openai import FakeEvent, _adapter, _collect


@pytest.mark.parametrize("ending", ["tool_use", "max_tokens"])
def test_later_failed_compaction_keeps_post_summary_context(monkeypatch, ending):
    from server.chat.engine import activity_labels

    original = _history()
    supplied = copy.deepcopy(original)
    replacement = [{"role": "user", "content": "Earlier context summary"}, *original[-8:]]
    calls = []

    async def compact(history, *args, **kwargs):
        calls.append(copy.deepcopy(history))
        if len(calls) == 1:
            return copy.deepcopy(replacement)
        raise compaction.CompactionError()

    async def route(*args, **kwargs):
        return "RESULT AFTER SUMMARY", []

    monkeypatch.setattr(compaction, "compact_messages_with_claude", compact)
    monkeypatch.setattr(compaction, "thresholds_for", lambda *args: (1, 1_000_000))
    monkeypatch.setattr("server.tool_executor.route_tool", route)
    monkeypatch.setattr(activity_labels, "start_label_task", lambda *args: None)

    class Adapter:
        provider = "test"

        def __init__(self):
            self.requests = []

        async def stream_round(self, messages, *args):
            self.requests.append(copy.deepcopy(messages))
            if len(self.requests) == 1:
                raise PromptTooLongError("prompt is too long")
            content = (
                [{"type": "tool_use", "id": "after-summary", "name": "probe", "input": {}}]
                if ending == "tool_use"
                else [{"type": "text", "text": "PARTIAL AFTER SUMMARY"}]
            )
            yield RoundResult(stop_reason=ending, content=content, usage=Usage())

    async def run():
        adapter = Adapter()
        ctx = TurnContext(
            session_id="latest-compaction-context",
            model_key="haiku",
            model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0",
            messages=supplied,
            adapter=adapter,
            policy=TurnPolicy(max_rounds=20, completion_gate=False),
            loop=asyncio.get_running_loop(),
            executor=None,
            cost_source="chat",
            memory_hooks=lambda messages: None,
            tool_exec_model_id="",
            tool_catalog=lambda: ([{"name": "probe", "input_schema": {"type": "object"}}], None),
        )
        frames = [frame async for frame in run_turn(ctx)]
        return frames, ctx, adapter

    frames, ctx, adapter = asyncio.run(run())
    _assert_failed_once(frames)
    assert len(adapter.requests) == len(calls) == 2
    assert supplied == original
    assert ctx.messages is not supplied
    assert ctx.messages == calls[-1]
    expected = "RESULT AFTER SUMMARY" if ending == "tool_use" else "PARTIAL AFTER SUMMARY"
    assert expected in json.dumps(ctx.messages)
    assert ctx.messages[: len(replacement)] == replacement


@pytest.mark.parametrize("source", ["request", "stream"])
def test_structured_openai_context_error_reaches_compaction(monkeypatch, source):
    response = httpx.Response(400, request=httpx.Request("POST", "https://mock.invalid"))
    if source == "request":
        event = BadRequestError(
            "The request exceeds its limit",
            response=response,
            body={"error": {"code": "context_length_exceeded"}},
        )
    else:
        event = [
            FakeEvent(
                type="response.failed",
                response=SimpleNamespace(
                    error=SimpleNamespace(
                        code="context_length_exceeded", message="The request exceeds its limit"
                    )
                ),
            )
        ]
    adapter, calls = _adapter(monkeypatch, [event])
    with pytest.raises(PromptTooLongError):
        asyncio.run(_collect(adapter.stream_round([], [], None, 0, False)))
    assert len(calls.captured) == 1


def test_context_length_error_text_is_recognized_without_a_code():
    assert _is_prompt_too_long("maximum context length exceeded")
    assert not _is_prompt_too_long("invalid max_output_tokens parameter")
    assert not _is_prompt_too_long("context length is available in Settings")
