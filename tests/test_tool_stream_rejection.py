"""Provider argument failures stop before tools run and retain billed usage."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from server.chat.engine import events as EV
from server.chat.engine.anthropic import AnthropicAdapter
from server.chat.engine.openai import _friendly_error
from tests.golden_harness import msg_end, msg_start
from tests.test_engine_openai import FakeEvent, _adapter, _collect, _completed, _gpt_turn
from tests.test_usage_capture import _Body, _chunk


def _start(item_id="item", call_id="call", name="ws_write_file"):
    return FakeEvent(
        type="response.output_item.added",
        item=FakeEvent(type="function_call", id=item_id, call_id=call_id, name=name, arguments=""),
    )


def _done(raw, item_id="item"):
    return FakeEvent(type="response.function_call_arguments.done", item_id=item_id, arguments=raw)


def _openai_events(monkeypatch, script):
    adapter, _ = _adapter(monkeypatch, [script])
    events = asyncio.run(_collect(adapter.stream_round([], [], None, 0, False)))
    return adapter, events


def _assert_rejected(events):
    assert isinstance(events[-1], EV.RoundError)
    assert events[-1].retryable is False
    assert not any(isinstance(event, EV.RoundResult) for event in events)


@pytest.mark.parametrize("raw", ["", " ", "{", '{"path":', "[]", "null", '"x"', "42", "true"])
def test_openai_invalid_arguments_never_become_an_empty_call(monkeypatch, raw):
    adapter, events = _openai_events(monkeypatch, [_start(), _done(raw), _completed(33, 7)])
    _assert_rejected(events)
    assert not any(isinstance(event, EV.ToolCall) for event in events)
    usage = adapter.inflight_usage()
    assert (usage.input_tokens, usage.output_tokens, usage.estimated) == (33, 7, ())


@pytest.mark.parametrize("ending", ["completed", "incomplete", "eof"])
def test_openai_requires_argument_completion_not_just_response_completion(monkeypatch, ending):
    script = [
        _start(),
        FakeEvent(
            type="response.function_call_arguments.delta", item_id="item", delta='{"path":"a"}'
        ),
    ]
    if ending == "completed":
        script.append(_completed())
    elif ending == "incomplete":
        script.append(
            FakeEvent(
                type="response.incomplete",
                response=FakeEvent(incomplete_details=FakeEvent(reason="max_output_tokens")),
            )
        )
    _, events = _openai_events(monkeypatch, script)
    _assert_rejected(events)
    assert not any(isinstance(event, EV.ToolCall) for event in events)


def test_openai_explicit_empty_arguments_remain_valid(monkeypatch):
    _, events = _openai_events(monkeypatch, [_start(name="git_status"), _done("{}"), _completed()])
    assert isinstance(events[-1], EV.RoundResult)
    assert events[-1].content == [
        {"type": "tool_use", "id": "call", "name": "git_status", "input": {}}
    ]


def test_duplicate_done_cannot_complete_another_pending_call(monkeypatch):
    _, events = _openai_events(
        monkeypatch,
        [
            _start("first", "first-call", "git_status"),
            _start("second", "second-call", "ws_write_file"),
            _done("{}", "first"),
            _done("{}", "first"),
            _completed(),
        ],
    )
    _assert_rejected(events)


def test_orphan_done_is_a_visible_error(monkeypatch):
    _, events = _openai_events(monkeypatch, [_done("{}", "never-started"), _completed()])
    _assert_rejected(events)


def test_conflicting_final_arguments_are_not_silently_substituted(monkeypatch):
    _, events = _openai_events(
        monkeypatch,
        [
            _start(),
            FakeEvent(
                type="response.function_call_arguments.delta", item_id="item", delta='{"path":"a"}'
            ),
            _done('{"path":"b"}'),
            _completed(),
        ],
    )
    _assert_rejected(events)


def test_incomplete_response_blocks_even_a_completed_tool_call(monkeypatch):
    adapter, events = _openai_events(
        monkeypatch,
        [
            _start(name="git_status"),
            _done("{}"),
            FakeEvent(
                type="response.incomplete",
                response=FakeEvent(
                    usage=FakeEvent(input_tokens=44, output_tokens=11),
                    incomplete_details=FakeEvent(reason="max_output_tokens"),
                ),
            ),
        ],
    )
    _assert_rejected(events)
    assert adapter.inflight_usage().output_tokens == 11


def test_content_filter_is_not_reported_as_an_output_token_limit(monkeypatch):
    _, events = _openai_events(
        monkeypatch,
        [
            FakeEvent(
                type="response.incomplete",
                response=FakeEvent(incomplete_details=FakeEvent(reason="content_filter")),
            )
        ],
    )
    _assert_rejected(events)
    assert "content_filter" in events[-1].message
    assert not any(isinstance(event, EV.Incomplete) for event in events)
    assert "token limit" not in events[-1].message.lower()


def test_mixed_valid_and_invalid_calls_never_reach_the_executor(monkeypatch):
    seen = []

    async def forbidden_batch(*args, **kwargs):
        seen.append(args)
        raise AssertionError("an invalid argument round must not execute")

    monkeypatch.setattr("server.tool_executor.execute_tool_batch", forbidden_batch)
    lines, responses = _gpt_turn(
        monkeypatch,
        [
            [
                _start("first", "first-call", "git_status"),
                _done("{}", "first"),
                _start("second", "second-call", "ws_write_file"),
                _done('{"path":', "second"),
                _completed(),
            ]
        ],
    )
    assert seen == []
    assert len(responses.captured) == 1
    assert any('"error"' in line for line in lines)
    assert lines[-1] == "[DONE]"


def _anthropic_events(script):
    async def run():
        adapter = object.__new__(AnthropicAdapter)
        adapter.model_key = "probe"
        adapter.model_id = "test-model"
        adapter.system_prompt = "system"
        adapter.system_static = adapter.system_dynamic = ""
        adapter.caching_on = False
        adapter.cache_ttl = None
        adapter.effort_label = None
        adapter._meta = {}
        adapter.max_tokens = 1024
        adapter._loop = asyncio.get_running_loop()
        adapter._inflight = None
        adapter._inflight_received = 0
        body = _Body([_chunk(event) for event in script])
        adapter._client = SimpleNamespace(
            invoke_model_with_response_stream=lambda **kwargs: {"body": body}
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            adapter._executor = pool
            events = [event async for event in adapter.stream_round([], [], None, 0, False)]
        return adapter, events

    return asyncio.run(run())


def _anthropic_start(initial=None):
    block = {"type": "tool_use", "id": "call", "name": "probe"}
    if initial is not None:
        block["input"] = initial
    return {"type": "content_block_start", "index": 0, "content_block": block}


def _anthropic_delta(raw):
    return {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": raw},
    }


@pytest.mark.parametrize("raw", ["", " ", "{", "[]", "null", "5"])
def test_anthropic_malformed_arguments_are_rejected_with_usage(raw):
    adapter, events = _anthropic_events(
        [
            msg_start(input_tokens=25),
            _anthropic_start({}),
            _anthropic_delta(raw),
            {"type": "content_block_stop", "index": 0},
            *msg_end("tool_use", output_tokens=12),
        ]
    )
    _assert_rejected(events)
    assert not any(isinstance(event, EV.ToolCall) for event in events)
    assert adapter.inflight_usage().output_tokens == 12


def test_anthropic_explicit_initial_empty_object_is_a_valid_zero_argument_call():
    _, events = _anthropic_events(
        [
            msg_start(),
            _anthropic_start({}),
            {"type": "content_block_stop", "index": 0},
            *msg_end("tool_use"),
        ]
    )
    assert isinstance(events[-1], EV.RoundResult)
    assert events[-1].content[-1]["input"] == {}


def test_anthropic_unclosed_tool_block_is_not_executable():
    _, events = _anthropic_events(
        [msg_start(), _anthropic_start({}), _anthropic_delta("{}"), *msg_end("tool_use")]
    )
    _assert_rejected(events)


def test_anthropic_conflicting_initial_input_and_deltas_are_rejected():
    _, events = _anthropic_events(
        [
            msg_start(),
            _anthropic_start({"path": "initial"}),
            _anthropic_delta('{"path":"different"}'),
            {"type": "content_block_stop", "index": 0},
            *msg_end("tool_use"),
        ]
    )
    _assert_rejected(events)


def test_anthropic_output_limit_never_executes_tools():
    adapter, events = _anthropic_events(
        [
            msg_start(),
            _anthropic_start({}),
            _anthropic_delta('{"path":'),
            *msg_end("max_tokens", output_tokens=99),
        ]
    )
    _assert_rejected(events)
    assert adapter.inflight_usage().output_tokens == 99


@pytest.mark.parametrize(
    "message",
    [
        "max_output_tokens must be less than or equal to 100",
        "Error code: 429 - Too many tokens per minute",
        "The maximum output is 401 tokens",
        "The prompt contains 403 tokens",
    ],
)
def test_token_parameters_and_limits_are_not_authentication_errors(message):
    assert "auth/access failed" not in _friendly_error(Exception(message)).lower()


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 401 - Unauthorized",
        "403 AccessDeniedException: not authorized",
        "invalid bearer token",
    ],
)
def test_real_authentication_errors_keep_actionable_advice(message):
    assert "auth/access failed" in _friendly_error(Exception(message)).lower()
