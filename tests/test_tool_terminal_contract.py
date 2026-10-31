"""Terminal response payloads and rejected-round counts must remain consistent."""

import asyncio
import copy
import json

import pytest

from server.chat.engine import events as EV
from server.chat.engine.local import LocalAdapter
from server.costs.capture import estimate_tokens
from tests.test_engine_openai import FakeEvent, _adapter, _collect, _completed, _gpt_turn
from tests.test_llama_server_backend import _fake_httpx
from tests.test_tool_stream_rejection import (
    _anthropic_delta,
    _anthropic_events,
    _anthropic_start,
    _assert_rejected,
    _done,
    _openai_events,
    _start,
)


def _item():
    return FakeEvent(
        type="function_call",
        id="item",
        call_id="call",
        name="git_status",
        arguments="{}",
        status="completed",
    )


def _terminal(output, usage=None):
    return FakeEvent(type="response.completed", response=FakeEvent(output=output, usage=usage))


@pytest.mark.parametrize("usage", [None, FakeEvent()])
def test_empty_usage_does_not_discard_actual_request_and_argument_estimates(monkeypatch, usage):
    adapter, events = _openai_events(
        monkeypatch, [_start(), _done('{"path":'), _terminal([], usage)]
    )
    _assert_rejected(events)
    received = adapter._inflight["received"] + sum(
        len(raw) for raw in adapter._inflight["args"].values()
    )
    result = adapter.inflight_usage()
    assert result.input_tokens == estimate_tokens(adapter._inflight["request_chars"])
    assert result.output_tokens == estimate_tokens(received)
    assert result.estimated == ("input", "output")


@pytest.mark.parametrize("shape", ["duplicate", "missing"])
def test_contradictory_final_calls_never_execute(monkeypatch, shape):
    seen = []

    async def route(name, args, **kwargs):
        seen.append((name, copy.deepcopy(args)))
        return "mock tool ran", []

    monkeypatch.setattr("server.tool_executor.route_tool", route)
    item = _item()
    output = [item, item] if shape == "duplicate" else []
    lines, _ = _gpt_turn(
        monkeypatch,
        [
            [_start(name="git_status"), _done("{}"), _terminal(output)],
            [FakeEvent(type="response.output_text.delta", delta="Finished."), _completed()],
        ],
    )
    assert seen == []
    assert any('"error"' in line for line in lines)
    assert lines[-1] == "[DONE]"


def test_matching_final_snapshot_is_not_a_duplicate_streamed_event(monkeypatch):
    item = _item()
    _, events = _openai_events(
        monkeypatch,
        [
            _start(name="git_status"),
            _done("{}"),
            FakeEvent(type="response.output_item.done", item=item),
            _terminal([item], FakeEvent(input_tokens=30, output_tokens=7)),
        ],
    )
    assert isinstance(events[-1], EV.RoundResult)
    assert len(events[-1].content) == 1
    assert events[-1].usage.output_tokens == 7


def test_duplicate_streamed_completion_is_refused(monkeypatch):
    done = FakeEvent(type="response.output_item.done", item=_item())
    _, events = _openai_events(
        monkeypatch, [_start(name="git_status"), _done("{}"), done, done, _completed()]
    )
    _assert_rejected(events)


def test_reported_input_without_output_preserves_an_explicit_output_estimate():
    raw = '{"path":'
    adapter, events = _anthropic_events(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 25}}},
            _anthropic_start({}),
            _anthropic_delta(raw),
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ]
    )
    _assert_rejected(events)
    usage = adapter.inflight_usage()
    assert usage.input_tokens == 25
    assert usage.output_tokens == estimate_tokens(len(raw))
    assert usage.estimated == ("output",)


def test_usage_estimate_includes_content_received_after_argument_rejection():
    raw, later = '{"path":', "x" * 80
    adapter, events = _anthropic_events(
        [
            {"type": "message_start", "message": {}},
            _anthropic_start({}),
            _anthropic_delta(raw),
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": later},
            },
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ]
    )
    _assert_rejected(events)
    usage = adapter.inflight_usage()
    assert usage.estimated == ("input", "output")
    assert usage.output_tokens == estimate_tokens(len(raw) + len(later))


@pytest.mark.parametrize("tool_calls", [None, [], {}])
def test_local_reported_tool_calls_require_a_complete_payload(monkeypatch, tool_calls):
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"tool_calls": tool_calls}}]}),
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":50,"completion_tokens":12}}',
        "data: [DONE]",
    ]
    _fake_httpx(monkeypatch, lines, {})
    adapter = LocalAdapter(
        model_key="probe",
        base_url="http://mock.invalid",
        system_prompt="system",
        thinking_level=None,
        tools_enabled=True,
    )
    events = asyncio.run(_collect(adapter.stream_round([], [], None, 0, False)))
    _assert_rejected(events)
    usage = adapter.inflight_usage()
    assert (usage.input_tokens, usage.output_tokens, usage.estimated) == (50, 12, ())
