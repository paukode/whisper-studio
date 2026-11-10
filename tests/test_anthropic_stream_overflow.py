"""Stream-time overflow follows fail-closed compaction, not transient retries."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from botocore.exceptions import ClientError

from server.chat.engine.anthropic import AnthropicAdapter
from server.chat.engine.events import RoundError, RoundResult, TextDelta, ToolCall
from server.infrastructure.errors import PromptTooLongError
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block


class _Body:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def __iter__(self):
        yield from FakeBedrockClient._stream(self.events)

    def close(self):
        self.closed = True


def _adapter(monkeypatch, body):
    client = SimpleNamespace(invoke_model_with_response_stream=Mock(return_value={"body": body}))
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: client)
    adapter = AnthropicAdapter(
        model_key="sonnet",
        model_id="test-model",
        system_prompt="s",
        system_static="",
        system_dynamic="",
        caching_on=False,
        cache_ttl="5m",
        effort_label=None,
        meta={},
    )
    return adapter, client


@pytest.mark.parametrize("source", ["envelope", "iterator", "typed"])
@pytest.mark.parametrize("partial", [False, True])
def test_stream_overflow_propagates_and_closes_body(monkeypatch, source, partial):
    error = {
        "envelope": {"validationException": {"message": "prompt is too long"}},
        "iterator": ClientError(
            {"Error": {"Code": "ValidationException", "Message": "prompt is too long"}},
            "InvokeModelWithResponseStream",
        ),
        "typed": PromptTooLongError("prompt is too long"),
    }[source]
    prefix = [msg_start(), *text_block("Partial text")] if partial else []
    body = _Body([*prefix, error])
    messages = [{"role": "user", "content": "Keep this prompt"}]
    original = copy.deepcopy(messages)
    seen = []

    async def go():
        adapter, client = _adapter(monkeypatch, body)
        with pytest.raises(PromptTooLongError):
            async for event in adapter.stream_round(messages, [], 0, 0, False):
                seen.append(event)
        client.invoke_model_with_response_stream.assert_called_once()
        usage = adapter.inflight_usage()
        if partial:
            assert usage is not None and usage.input_tokens == 100
        else:
            assert usage is None

    asyncio.run(go())
    assert body.closed
    assert messages == original
    assert not any(isinstance(event, (RoundError, RoundResult, ToolCall)) for event in seen)
    assert "".join(event.text for event in seen if isinstance(event, TextDelta)) == (
        "Partial text" if partial else ""
    )


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        ({"validationException": {"message": "invalid tool_choice"}}, False),
        ({"throttlingException": {"message": "rate exceeded"}}, True),
    ],
)
def test_non_overflow_stream_error_keeps_existing_retry_policy(monkeypatch, error, retryable):
    body = _Body([error])

    async def go():
        adapter, _ = _adapter(monkeypatch, body)
        return [event async for event in adapter.stream_round([], [], 0, 0, False)]

    events = asyncio.run(go())
    assert body.closed
    assert len(events) == 1 and isinstance(events[0], RoundError)
    assert events[0].retryable is retryable


def test_invalid_tool_arguments_stay_terminal_even_if_stream_later_overflows(monkeypatch):
    body = _Body(
        [
            msg_start(),
            {"type": "content_block_stop", "index": 0},
            {"validationException": {"message": "prompt is too long"}},
        ]
    )

    async def go():
        adapter, _ = _adapter(monkeypatch, body)
        events = [event async for event in adapter.stream_round([], [], 0, 0, False)]
        assert adapter.inflight_usage().input_tokens == 100
        return events

    events = asyncio.run(go())
    assert body.closed
    assert len(events) == 1 and isinstance(events[0], RoundError)
    assert not events[0].retryable
    assert "Orphan or duplicate content block event" in events[0].message


def test_headless_native_stream_overflow_stops_without_distillation(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.chat import compaction, infra
    from server.exec.headless import run_headless_turn
    from server.infrastructure import config
    from server.infrastructure.sessions import router

    cfg = copy.deepcopy(config.load_config())
    cfg["model_mode"] = "cloud"
    cfg["chat_models"]["sonnet"] = "global.anthropic.claude-sonnet-test"
    cfg["chat_model_meta"]["sonnet"] = {}
    for module in (config, infra):
        monkeypatch.setattr(module, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(compaction, "thresholds_for", lambda *a, **k: (1, 1_000_000))
    client = FakeBedrockClient(
        [
            [msg_start(), *text_block("Partial headless result"), *msg_end("max_tokens")],
            *[[{"validationException": {"message": "prompt is too long"}}] for _ in range(3)],
        ]
    )
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: client)
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool", lambda *a, **k: ([], [], 0)
    )
    summary = Mock(side_effect=AssertionError("no summary without a safe history split"))
    monkeypatch.setattr(infra, "_get_bedrock_client", summary)
    distill = AsyncMock(side_effect=AssertionError("no distillation after compaction failure"))
    monkeypatch.setattr("server.agents.runtime_support.distill_run_output", distill)
    sid = "headless-native-stream-overflow"
    timestamp = "2026-01-01T00:00:00+00:00"
    monkeypatch.setattr("server.exec.headless._utc_now_iso", lambda: timestamp)

    async def go():
        return [
            event
            async for event in run_headless_turn(
                "Preserve this prompt",
                model_key="sonnet",
                session_id=sid,
                output_schema={"type": "object"},
                max_rounds=20,
            )
        ]

    events = asyncio.run(go())
    assert events[-1] == {"type": "done", "status": "failed", "session_id": sid}
    assert sum(event["type"] == "done" for event in events) == 1
    assert any(event.get("text") == "Partial headless result" for event in events)
    assert any("Context compaction failed" in event.get("message", "") for event in events)
    assert not any(event["type"] == "structured" for event in events)
    assert len(client.requests) == 2
    summary.assert_not_called()
    distill.assert_not_called()
    app = FastAPI()
    app.include_router(router)
    saved = TestClient(app).get(f"/api/sessions/{sid}").json()["chatHistory"]
    assert saved == [{"role": "user", "content": "Preserve this prompt", "timestamp": timestamp}]
