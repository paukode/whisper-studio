"""Record the side-task model calls the turn engine never sees.

Titles, compaction summaries, the auto-mode classifier, the permission
explainer, memory recall, query rewriting, the goal evaluator, index passes
(including Cohere embed and rerank), OCR, transcript condensation,
structured-output distillation and the like call Bedrock directly. Each is
billed like a chat round, so each is logged once, here, with its ``source``,
the counts from its own response payload (or characters / 4 of what was
posted and received when the payload has none), and the session it served
when there is one.

Recording runs in the thread that made the call, right after the response is
read, so a call whose awaiting coroutine timed out or was cancelled is still
logged. A failure to record is logged loudly and never breaks the call.
"""

from __future__ import annotations

import json
import logging

from server.costs import capture, tracker
from server.costs.sources import require_source

log = logging.getLogger("whisper-studio")


def model_key_for(model_id: str) -> str:
    """The chat_models key a provider id belongs to (pricing is keyed by it),
    else the id itself, which then shows up unpriced rather than silently
    borrowing another model's rate."""
    if not model_id:
        return "unknown"
    try:
        from server.infrastructure.config import load_config

        for key, value in (load_config().get("chat_models") or {}).items():
            mid = value.get("id") if isinstance(value, dict) else value
            if mid == model_id:
                return key
    except Exception as e:  # noqa: BLE001 - the id still names the row
        log.warning("Cost log: could not resolve a model key for %r: %s", model_id, e)
    return model_id


def record_counts(counts: dict, *, model_key: str, source: str, session_id: str = "") -> None:
    """One call's counts (server.costs.capture shape) into the cost log."""
    tracker.record_turn(
        session_id or "",
        0,
        model_key,
        counts["input_tokens"],
        counts["output_tokens"],
        cache_read_tokens=counts["cache_read_tokens"],
        cache_creation_tokens=counts["cache_write_tokens"],
        source=source,
        estimated=counts["estimated"],
        detail=counts["detail"],
    )


def record_invoke(
    response: dict, payload: dict, *, model_id: str, body: str, source: str, session_id: str = ""
) -> None:
    """Log one non-streaming invoke_model call with an Anthropic body."""
    require_source(source)
    try:
        key = model_key_for(model_id)
        counts = capture.claude_invoke_counts(response, payload, body, label=f"{source} ({key})")
        record_counts(counts, model_key=key, source=source, session_id=session_id)
    except Exception as e:  # noqa: BLE001 - a cost write never breaks the call
        log.error("Cost log: could not record a %s call: %s", source, e)


def invoke_claude(
    client, *, model_id: str, body: str, source: str, session_id: str = "", **kwargs
) -> dict:
    """``client.invoke_model`` with an Anthropic body, logged; returns the
    parsed response payload."""
    require_source(source)
    response = client.invoke_model(modelId=model_id, body=body, **kwargs)
    payload = json.loads(response["body"].read())
    record_invoke(
        response, payload, model_id=model_id, body=body, source=source, session_id=session_id
    )
    return payload


def invoke_input_billed(
    client, *, model_id: str, body: str, source: str, session_id: str = ""
) -> dict:
    """``client.invoke_model`` for a model billed on its input only (Cohere
    embed and rerank), logged; returns the parsed payload. A call that
    returned is logged even when its body cannot be read."""
    require_source(source)
    response = client.invoke_model(modelId=model_id, body=body)
    payload: dict = {}
    try:
        payload = json.loads(response["body"].read())
        return payload
    finally:
        try:
            counts = capture.input_billed_counts(response, payload, body)
            record_counts(
                counts, model_key=model_key_for(model_id), source=source, session_id=session_id
            )
        except Exception as e:  # noqa: BLE001 - a cost write never breaks the call
            log.error("Cost log: could not record a %s call: %s", source, e)


def record_responses(
    response, *, model_key: str, request: dict, source: str, session_id: str = ""
) -> None:
    """Log one non-streaming OpenAI Responses call on bedrock-mantle."""
    require_source(source)
    try:
        received = len(getattr(response, "output_text", "") or "")
        counts = capture.responses_counts(
            getattr(response, "usage", None), capture.json_chars(request), received
        )
        record_counts(counts, model_key=model_key, source=source, session_id=session_id)
    except Exception as e:  # noqa: BLE001 - a cost write never breaks the call
        log.error("Cost log: could not record a %s call: %s", source, e)


class ClaudeStreamRecorder:
    """Logs one streamed invoke_model_with_response_stream call (the /btw side
    question): feed every decoded event to ``observe`` and received text to
    ``received``, then call ``finish`` once, whether the stream completed,
    failed or was abandoned."""

    def __init__(self, *, model_id: str, body: str, source: str, session_id: str = ""):
        require_source(source)
        self._meter = capture.ClaudeStreamUsage()
        self._model_id = model_id
        self._body_chars = len(body)
        self._source = source
        self._session_id = session_id
        self._chars = 0
        self._completed = False
        self._done = False

    def observe(self, data: dict) -> None:
        self._meter.observe(data)
        if data.get("type") == "message_stop":
            self._completed = True

    def received(self, text: str) -> None:
        self._chars += len(text or "")

    def finish(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            key = model_key_for(self._model_id)
            label = f"{self._source} ({key})"
            if self._completed:
                counts = self._meter.result(self._body_chars, self._chars, label=label)
            else:
                counts = self._meter.partial(self._chars, label=label)
            if counts is not None:
                record_counts(
                    counts, model_key=key, source=self._source, session_id=self._session_id
                )
        except Exception as e:  # noqa: BLE001 - a cost write never breaks the stream
            log.error("Cost log: could not record a %s stream: %s", self._source, e)
