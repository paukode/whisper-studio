"""Local tool arguments remain explicit objects and failures retain usage."""

import asyncio

import pytest

from server.chat.engine import events as EV
from server.chat.engine.local import LocalAdapter
from server.local import server_stream
from server.tool_inputs import ToolInputError


@pytest.mark.parametrize("raw", ["", " ", "{", '{"path":', "null", "[]", '"text"', "1", "true"])
def test_local_accumulator_rejects_invalid_objects(raw):
    calls = server_stream._CallAccumulator()
    calls.feed(
        [{"index": 0, "id": "call", "function": {"name": "ws_write_file", "arguments": raw}}]
    )
    with pytest.raises(ToolInputError):
        calls.finish()


def test_local_accumulator_does_not_drop_a_valid_sibling_on_parse_failure():
    calls = server_stream._CallAccumulator()
    calls.feed(
        [
            {"index": 0, "id": "good", "function": {"name": "git_status", "arguments": "{}"}},
            {"index": 1, "id": "bad", "function": {"name": "ws_write_file", "arguments": "{"}},
        ]
    )
    with pytest.raises(ToolInputError):
        calls.finish()


def test_local_accumulator_rejects_duplicate_ids():
    calls = server_stream._CallAccumulator()
    calls.feed(
        [
            {"index": 0, "id": "same", "function": {"name": "git_status", "arguments": "{}"}},
            {"index": 1, "id": "same", "function": {"name": "git_status", "arguments": "{}"}},
        ]
    )
    with pytest.raises(ToolInputError):
        calls.finish()


def test_local_accumulator_preserves_legitimate_empty_calls():
    calls = server_stream._CallAccumulator()
    calls.feed([{"index": 0, "id": "call", "function": {"name": "git_status", "arguments": "{}"}}])
    assert calls.finish() == [{"id": "call", "name": "git_status", "input": {}}]


def test_local_accumulator_never_coerces_property_types():
    calls = server_stream._CallAccumulator()
    calls.feed(
        [
            {
                "index": 0,
                "id": "call",
                "function": {"name": "probe", "arguments": '{"n":"5","enabled":"true"}'},
            }
        ]
    )
    assert calls.finish()[0]["input"] == {"n": "5", "enabled": "true"}


@pytest.mark.parametrize("reason", ["tool_calls", "length"])
def test_rejected_local_argument_round_keeps_reported_usage_and_never_retries(monkeypatch, reason):
    attempts = []

    async def stream(base_url, payload):
        attempts.append(payload)
        yield (
            "done",
            {
                "calls": [],
                "argument_error": "Malformed tool arguments.",
                "call_names": ["ws_write_file"],
                "argument_chars": 17,
                "finish_reason": reason,
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
            },
        )

    monkeypatch.setattr(server_stream, "_stream_round", stream)
    adapter = LocalAdapter(
        model_key="probe",
        base_url="http://127.0.0.1:1",
        system_prompt="system",
        thinking_level=None,
        tools_enabled=True,
    )

    async def run():
        return [event async for event in adapter.stream_round([], [], None, 0, False)]

    events = asyncio.run(run())
    assert len(attempts) == 1
    assert isinstance(events[-1], EV.RoundError)
    assert not events[-1].retryable
    assert not any(isinstance(event, (EV.RoundResult, EV.ToolCall)) for event in events)
    usage = adapter.inflight_usage()
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (20, 40, 80)
    assert usage.estimated == ()
