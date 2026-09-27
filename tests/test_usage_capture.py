"""Token counts come from the provider's payload; estimates are characters / 4.

Claude on Bedrock reports twice (the Anthropic usage fields and Bedrock's
amazon-bedrock-invocationMetrics trailer); message_delta output is cumulative.
An attempt that dies mid-stream, is retried, or is stopped was still billed
for what the provider processed, so it is recorded once, as its own row.
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from server.chat.engine import events as EV
from server.chat.engine.anthropic import AnthropicAdapter
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn
from server.costs import capture, tracker

_START_USAGE = {
    "input_tokens": 12,
    "cache_creation_input_tokens": 800,
    "cache_read_input_tokens": 95_000,
    "output_tokens": 1,
}


def _chunk(obj: dict) -> dict:
    return {"chunk": {"bytes": json.dumps(obj).encode("utf-8")}}


_THOUGHT = "Planning the answer."
_ANSWER = "Hello there, twenty chars"


def _claude_stream(*, deltas=(420,), metrics=None, error_after_thinking=False):
    ev = [
        _chunk({"type": "message_start", "message": {"usage": dict(_START_USAGE)}}),
        _chunk({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}),
        _chunk(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": _THOUGHT},
            }
        ),
    ]
    if error_after_thinking:
        # A transient fault before any answer text: the engine retries.
        return [*ev, {"internalServerException": {"message": "boom mid-stream"}}]
    ev += [
        _chunk({"type": "content_block_stop", "index": 0}),
        _chunk({"type": "content_block_start", "index": 1, "content_block": {"type": "text"}}),
        _chunk(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": _ANSWER},
            }
        ),
        _chunk({"type": "content_block_stop", "index": 1}),
    ]
    for cumulative in deltas:
        ev.append(
            _chunk(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": cumulative},
                }
            )
        )
    stop = {"type": "message_stop"}
    if metrics is not None:
        stop["amazon-bedrock-invocationMetrics"] = metrics
    ev.append(_chunk(stop))
    return ev


class _Body:
    def __init__(self, events):
        self._events = events

    def __iter__(self):
        return iter(self._events)

    def close(self):
        pass


class _Client:
    def __init__(self, streams):
        self.streams = list(streams)

    def invoke_model_with_response_stream(self, **kw):
        return {"body": _Body(self.streams.pop(0))}


def _claude(streams) -> AnthropicAdapter:
    a = object.__new__(AnthropicAdapter)  # no boto3 client
    a.model_key = "opus5.0"
    a.model_id = "global.anthropic.claude-opus-5"
    a.system_prompt = "sys"
    a.system_static = a.system_dynamic = ""
    a.caching_on = False
    a.cache_ttl = None
    a.effort_label = None
    a.force_skill = None
    a._meta = {}
    a.max_tokens = 1000
    a._client = _Client(streams)
    a._executor = ThreadPoolExecutor(max_workers=2)
    a._inflight = None
    a._inflight_received = 0
    return a


def _one_round(adapter):
    async def go():
        adapter._loop = asyncio.get_running_loop()
        return [ev async for ev in adapter.stream_round([], [], None, 0, False)]

    return asyncio.run(go())


def test_cumulative_message_deltas_count_the_last_value_not_the_sum():
    (result,) = [
        e
        for e in _one_round(_claude([_claude_stream(deltas=(180, 420))]))
        if isinstance(e, EV.RoundResult)
    ]
    assert result.usage.output_tokens == 420
    assert result.usage.input_tokens == 12
    assert (result.usage.cache_read_tokens, result.usage.cache_write_tokens) == (95_000, 800)
    assert result.usage.estimated == () and result.usage.detail is None


def test_a_stream_that_ends_with_no_output_count_estimates_only_the_output():
    stream = [e for e in _claude_stream() if b"message_delta" not in e["chunk"]["bytes"]]
    usage = _one_round(_claude([stream]))[-1].usage
    # message_start's output_tokens (1) is a placeholder, not the count.
    assert usage.output_tokens == capture.estimate_tokens(len(_THOUGHT + _ANSWER))
    assert usage.estimated == ("output",)
    assert (usage.input_tokens, usage.cache_read_tokens) == (12, 95_000)
    trailer = {"inputTokenCount": 12, "outputTokenCount": 77}
    stream[-1] = _chunk({"type": "message_stop", "amazon-bedrock-invocationMetrics": trailer})
    usage = _one_round(_claude([stream]))[-1].usage
    assert (usage.output_tokens, usage.estimated, usage.detail) == (77, (), None)


def test_the_bedrock_trailer_is_authoritative_and_a_disagreement_keeps_both():
    metrics = {
        "inputTokenCount": 15,
        "outputTokenCount": 420,
        "cacheReadInputTokenCount": 95_000,
        "cacheWriteInputTokenCount": 800,
    }
    events = _one_round(_claude([_claude_stream(metrics=metrics)]))
    usage = events[-1].usage
    assert usage.input_tokens == 15  # what Bedrock metered
    assert usage.detail == {
        "bedrock_metrics": {
            "input_tokens": 15,
            "output_tokens": 420,
            "cache_read_tokens": 95_000,
            "cache_write_tokens": 800,
        },
        "anthropic_usage": {
            "input_tokens": 12,
            "output_tokens": 420,
            "cache_read_tokens": 95_000,
            "cache_write_tokens": 800,
        },
    }
    agreeing = dict(metrics, inputTokenCount=12)
    assert _one_round(_claude([_claude_stream(metrics=agreeing)]))[-1].usage.detail is None


def _turn(adapter, session_id, *, stop_after=None):
    """Drive one engine turn; ``stop_after`` names a frame key after which
    the user presses Stop (the SSE chain closes the generator there)."""

    async def go():
        adapter._loop = asyncio.get_running_loop()
        ctx = TurnContext(
            session_id=session_id,
            model_key=adapter.model_key,
            model_id=adapter.model_id,
            messages=[{"role": "user", "content": "q"}],
            adapter=adapter,
            policy=TurnPolicy(max_rounds=4, completion_gate=False),
            loop=asyncio.get_running_loop(),
            executor=adapter._executor,
            cost_source="chat",
            tool_exec_model_id="",
            memory_hooks=lambda msgs: None,
            tool_catalog=lambda: ([], None),
        )
        gen = run_turn(ctx)
        out = []
        async for chunk in gen:
            out.append(chunk)
            if stop_after and stop_after in chunk:
                await gen.aclose()  # the user pressed Stop
                break
        return out

    return asyncio.run(go())


@pytest.fixture
def _quiet_turn(monkeypatch):
    monkeypatch.setattr("server.costs.budget.check_budget", lambda sid: None)
    monkeypatch.setattr("server.costs.budget.check_budget_soft", lambda sid, fraction=0.9: None)
    monkeypatch.setattr("server.chat.engine.runner._ROUND_RETRY_BACKOFF_S", 0)


def test_a_retried_attempt_is_recorded_once_with_its_output_estimated(_quiet_turn):
    adapter = _claude([_claude_stream(error_after_thinking=True), _claude_stream()])
    _turn(adapter, "retry-sess")
    failed, completed = tracker.get_session_costs("retry-sess")
    # The failed attempt: the prompt counts message_start reported, the
    # output as characters / 4 of what arrived before the fault.
    assert (failed["input_tokens"], failed["cache_read_tokens"]) == (12, 95_000)
    assert failed["output_tokens"] == capture.estimate_tokens(len(_THOUGHT))
    assert (failed["count_source"], failed["estimated_fields"]) == ("estimated", "output")
    assert (completed["output_tokens"], completed["count_source"]) == (420, "reported")


def test_a_stopped_round_is_recorded(_quiet_turn):
    adapter = _claude([_claude_stream()])
    _turn(adapter, "stop-sess", stop_after='"text"')
    (row,) = tracker.get_session_costs("stop-sess")
    assert row["input_tokens"] == 12 and row["estimated_fields"] == "output"


def test_a_round_stopped_while_its_usage_frame_is_sent_is_still_recorded(_quiet_turn):
    adapter = _claude([_claude_stream()])
    _turn(adapter, "usage-stop", stop_after='"usage"')
    (row,) = tracker.get_session_costs("usage-stop")
    assert (row["output_tokens"], row["count_source"]) == (420, "reported")


def test_a_stream_that_fails_before_message_start_records_nothing():
    adapter = _claude([[{"throttlingException": {"message": "slow down"}}]])
    events = _one_round(adapter)
    assert isinstance(events[-1], EV.RoundError)
    assert adapter.inflight_usage() is None


# ── GPT on mantle ────────────────────────────────────────────────────────────


class _Ev(SimpleNamespace):
    pass


_SUMMARY = [
    _Ev(type="response.created"),
    _Ev(type="response.in_progress"),
    _Ev(type="response.reasoning_summary_text.delta", delta="Weighing the options."),
]


class _GptStream:
    """Scripted Responses events, then ``tail``: "hang" (still reasoning when
    the user stops), an exception (the stream breaks), or the end."""

    def __init__(self, events, tail=None):
        self._events, self._tail = list(events), tail

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
            if self._tail == "hang":
                await asyncio.Event().wait()
            elif isinstance(self._tail, BaseException):
                raise self._tail

        return gen()

    async def close(self):
        pass


class _GptResponses:
    def __init__(self, rounds):
        self.rounds, self.posted = list(rounds), []

    async def create(self, **request):
        self.posted.append(request)
        nxt = self.rounds.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def _gpt(monkeypatch, rounds):
    import server.openai_bedrock.runtime as oai
    from server.chat.engine.openai import OpenAIResponsesAdapter

    responses = _GptResponses(rounds)
    monkeypatch.setattr(oai, "build_client", lambda region: SimpleNamespace(responses=responses))
    monkeypatch.setattr(oai, "region_for", lambda mk: "us-east-2")
    adapter = OpenAIResponsesAdapter(
        model_key="gpt5.6-sol",
        model_id="openai.gpt-5.6-sol",
        system_prompt="sys",
        effort_label="high",
        session_id="gpt-sess",
    )
    adapter._executor = ThreadPoolExecutor(max_workers=1)
    return adapter, responses


def _gpt_completed():
    usage = _Ev(input_tokens=900, output_tokens=30, input_tokens_details=_Ev(cached_tokens=0))
    return _Ev(type="response.completed", response=_Ev(usage=usage))


def test_a_gpt_round_stopped_while_it_is_still_reasoning_bills_its_input(_quiet_turn, monkeypatch):
    adapter, responses = _gpt(monkeypatch, [_GptStream(_SUMMARY, tail="hang")])
    _turn(adapter, "gpt-stop", stop_after='"thinking"')
    (row,) = tracker.get_session_costs("gpt-stop")
    # Nothing billable arrived yet (a reasoning summary is not billed
    # output), but the posted prompt was being processed.
    assert row["input_tokens"] == capture.estimate_tokens(capture.json_chars(responses.posted[0]))
    assert row["output_tokens"] == 0
    assert (row["count_source"], row["estimated_fields"]) == ("estimated", "input,output")


def test_a_gpt_stream_that_breaks_while_reasoning_is_recorded_then_retried(
    _quiet_turn, monkeypatch
):
    broken = _GptStream(_SUMMARY, tail=RuntimeError("The server had an error"))
    done = _GptStream([_Ev(type="response.output_text.delta", delta="Hi"), _gpt_completed()])
    adapter, responses = _gpt(monkeypatch, [broken, done])
    _turn(adapter, "gpt-retry")
    failed, completed = tracker.get_session_costs("gpt-retry")
    assert failed["input_tokens"] == capture.estimate_tokens(
        capture.json_chars(responses.posted[0])
    )
    assert failed["count_source"] == "estimated"
    assert (completed["input_tokens"], completed["count_source"]) == (900, "reported")


def test_a_gpt_stream_that_breaks_before_any_event_records_nothing(monkeypatch):
    adapter, _ = _gpt(monkeypatch, [_GptStream([], tail=RuntimeError("connection reset"))])

    async def go():
        return [e async for e in adapter.stream_round([], [], None, 0, False)]

    assert isinstance(asyncio.run(go())[-1], EV.RoundError)
    assert adapter.inflight_usage() is None


def test_a_truncated_gpt_round_takes_its_counts_from_the_incomplete_payload(monkeypatch):
    # The output cap: response.incomplete ends the round with the usage it
    # billed, reasoning included. Characters / 4 of the visible text would
    # count 100 output tokens of 128,000.
    usage = _Ev(
        input_tokens=50_000,
        output_tokens=128_000,
        input_tokens_details=_Ev(cached_tokens=48_000),
    )
    stream = _GptStream(
        [
            _Ev(type="response.created"),
            _Ev(type="response.output_text.delta", delta="x" * 400),
            _Ev(
                type="response.incomplete",
                response=_Ev(usage=usage, incomplete_details=_Ev(reason="max_output_tokens")),
            ),
        ]
    )
    adapter, _ = _gpt(monkeypatch, [stream])

    async def go():
        return [e async for e in adapter.stream_round([], [], None, 0, False)]

    events = asyncio.run(go())
    assert any(isinstance(e, EV.Incomplete) for e in events)
    (result,) = [e for e in events if isinstance(e, EV.RoundResult)]
    u = result.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens) == (50_000, 128_000, 48_000)
    assert u.estimated == ()


def test_a_failed_gpt_response_with_usage_is_reported():
    usage = SimpleNamespace(
        input_tokens=5000,
        output_tokens=40,
        input_tokens_details=SimpleNamespace(cached_tokens=4000),
    )
    got = capture.responses_counts(usage, 999, 999)
    assert got["input_tokens"] == 5000 and got["cache_read_tokens"] == 4000
    assert got["estimated"] == ()
    assert capture.responses_counts(None, 400, 40)["estimated"] == ("input", "output")


def _mantle_usage(**extra):
    """A Responses usage as the SDK parses it, with mantle's undeclared
    cache-write count wherever ``extra`` puts it."""
    from openai.types.responses import ResponseUsage

    details = {"cached_tokens": 46_000, **extra.pop("details", {})}
    return ResponseUsage.model_validate(
        {
            "input_tokens": 51_300,
            "output_tokens": 410,
            "total_tokens": 51_710,
            "input_tokens_details": details,
            "output_tokens_details": {"reasoning_tokens": 120},
            **extra,
        }
    )


def test_a_gpt_cache_write_is_taken_from_the_mantle_usage():
    # Cost Explorer bills almost all of GPT's uncached prompt as 30-minute
    # cache writes; the app recorded 0 for every GPT row.
    beside_cached = capture.responses_counts(
        _mantle_usage(details={"cache_write_tokens": 5_230}), 0, 0
    )
    on_usage = capture.responses_counts(_mantle_usage(cache_write_tokens=5_230), 0, 0)
    for got in (beside_cached, on_usage):
        assert (got["input_tokens"], got["cache_read_tokens"], got["cache_write_tokens"]) == (
            51_300,
            46_000,
            5_230,
        )
        assert got["estimated"] == ()
    assert capture.responses_counts(_mantle_usage(), 0, 0)["cache_write_tokens"] == 0


def test_a_completed_gpt_round_records_its_cache_write(_quiet_turn, monkeypatch):
    usage = _mantle_usage(details={"cache_write_tokens": 5_230})
    done = _GptStream(
        [
            _Ev(type="response.output_text.delta", delta="Hi"),
            _Ev(type="response.completed", response=_Ev(usage=usage)),
        ]
    )
    adapter, _ = _gpt(monkeypatch, [done])
    _turn(adapter, "gpt-write")
    (row,) = tracker.get_session_costs("gpt-write")
    assert (row["input_tokens"], row["cache_read_tokens"], row["cache_creation_tokens"]) == (
        51_300,
        46_000,
        5_230,
    )


def test_the_usage_frame_prices_a_long_context_round_at_the_long_rate(_quiet_turn, monkeypatch):
    usage = _Ev(
        input_tokens=300_000,
        output_tokens=100,
        input_tokens_details=_Ev(cached_tokens=290_000),
    )
    done = _GptStream(
        [
            _Ev(type="response.output_text.delta", delta="Hi"),
            _Ev(type="response.completed", response=_Ev(usage=usage)),
        ]
    )
    adapter, _ = _gpt(monkeypatch, [done])
    frames = [
        json.loads(line[len("data: ") :])
        for chunk in _turn(adapter, "gpt-long")
        for line in chunk.splitlines()
        if line.startswith("data: {")
    ]
    (frame,) = [f["usage"] for f in frames if "usage" in f]
    long_cost = tracker.call_cost("gpt5.6-sol", 300_000, 100, 290_000)
    assert frame["estimated_cost_usd"] == pytest.approx(long_cost, abs=1e-6)
    assert long_cost > tracker.estimate_cost("gpt5.6-sol", 300_000, 100, 290_000)


# ── side-task invoke_model ───────────────────────────────────────────────────


def test_invoke_model_counts_prefer_bedrock_headers_and_fall_back_to_chars():
    body = json.dumps({"messages": [{"role": "user", "content": "x" * 400}]})
    payload = {
        "content": [{"type": "text", "text": "y" * 40}],
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }
    headers = {
        "ResponseMetadata": {
            "HTTPHeaders": {
                "x-amzn-bedrock-input-token-count": "100",
                "x-amzn-bedrock-output-token-count": "10",
            }
        }
    }
    agree = capture.claude_invoke_counts(headers, payload, body, label="t")
    assert (agree["input_tokens"], agree["output_tokens"], agree["detail"]) == (100, 10, None)
    headers["ResponseMetadata"]["HTTPHeaders"]["x-amzn-bedrock-input-token-count"] = "130"
    differ = capture.claude_invoke_counts(headers, payload, body, label="t")
    assert differ["input_tokens"] == 130 and set(differ["detail"]) == {
        "bedrock_metrics",
        "anthropic_usage",
    }
    bare = capture.claude_invoke_counts({}, {"content": payload["content"]}, body, label="t")
    assert bare["input_tokens"] == capture.estimate_tokens(len(body))
    assert bare["output_tokens"] == capture.estimate_tokens(40)
    assert bare["estimated"] == ("input", "output")


def test_the_estimate_is_characters_over_four():
    assert [capture.estimate_tokens(n) for n in (0, 1, 4, 5, 400)] == [0, 1, 1, 2, 100]
