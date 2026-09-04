"""Mid-stream transient fault rescue in the unified turn loop.

Regression coverage for the "turn dies after N tool rounds with a bare
provider error, retry works instantly" failure observed live: bedrock-mantle
returned HTTP 200 for round 5 and then killed the Responses stream with
OpenAI's stock server_error. The SDK retry layer only covers the request, so
a 200 that dies mid-stream used to discard every completed tool round of the
turn. The engine must re-run the failed round (bounded, and only while no
answer text has streamed), while non-retryable errors still end the turn.
"""

import pytest

import server.chat.engine.runner as runner_mod
from server.chat.engine.openai import _is_transient
from server.infrastructure.errors import classify_bedrock_error
from tests.golden_harness import (
    FakeBedrockClient,
    msg_end,
    msg_start,
    run_chat_turn,
    text_block,
)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(runner_mod, "_ROUND_RETRY_BACKOFF_S", 0.0)


def _stream_error(key="internalServerException", message="boom"):
    # Transport-level event, delivered outside the chunk envelope exactly as
    # Bedrock does (see FakeBedrockClient).
    return {key: {"message": message}}


def test_transient_midstream_error_retries_the_round(monkeypatch):
    # Round 1 dies mid-stream with a retryable fault before any output; the
    # re-run answers. The user sees a retry status, the answer, ONE [DONE],
    # and no error frame.
    client = FakeBedrockClient(
        [
            [_stream_error()],
            [msg_start(), *text_block("Recovered answer."), *msg_end(stop_reason="end_turn")],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "go"})
    joined = "\n".join(lines)

    assert "Recovered answer." in joined
    assert lines[-1] == "[DONE]"
    assert lines.count("[DONE]") == 1
    assert '"error"' not in joined
    assert "Model stream failed; retrying..." in joined
    assert len(client.requests) == 2


def test_retry_is_bounded_then_surfaces_the_error(monkeypatch):
    # A provider that fails every round gets the initial attempt plus
    # _ROUND_RETRIES_MAX re-runs, then the error surfaces terminally.
    client = FakeBedrockClient([[_stream_error()] for _ in range(6)])
    lines = run_chat_turn(monkeypatch, client, {"question": "go"})
    joined = "\n".join(lines)

    assert lines[-1] == "[DONE]"
    assert '"error"' in joined
    assert len(client.requests) == 1 + runner_mod._ROUND_RETRIES_MAX


def test_no_retry_after_answer_text_streamed(monkeypatch):
    # A retryable fault AFTER text reached the client must not re-run the
    # round — the re-run would duplicate the streamed answer.
    client = FakeBedrockClient(
        [
            [msg_start(), *text_block("partial ans"), _stream_error()],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "go"})
    joined = "\n".join(lines)

    assert "partial ans" in joined
    assert '"error"' in joined
    assert lines[-1] == "[DONE]"
    assert len(client.requests) == 1


def test_non_retryable_error_is_terminal(monkeypatch):
    client = FakeBedrockClient(
        [
            [_stream_error("validationException", "tool schema rejected")],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "go"})
    joined = "\n".join(lines)

    assert '"error"' in joined
    assert lines[-1] == "[DONE]"
    assert len(client.requests) == 1


def test_bedrock_stream_faults_classify_as_retryable():
    # AWS documents both as retryable; they used to fall into the default
    # non-retryable branch.
    for name in ("InternalServerException", "ModelStreamErrorException"):
        err = classify_bedrock_error(Exception(f"{name}: transient blip"))
        assert err.is_retryable, name
    assert not classify_bedrock_error(Exception("AccessDeniedException: no")).is_retryable


def test_openai_transient_marker_classification():
    # The exact live failure text, plus the obvious 5xx/throttle shapes.
    assert _is_transient("The server had an error while processing your request. Sorry about that!")
    assert _is_transient("Error code: 503 - service unavailable")
    assert _is_transient("Rate limit reached, too many requests")
    assert _is_transient("peer closed connection without sending complete message body")
    # Auth/config failures must surface immediately, not burn retries.
    assert not _is_transient("You don't have access to the model with the specified model ID.")
