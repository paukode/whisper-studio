"""Token counts from the provider's own payload, and the one estimate rule.

The rule (user decision 2026-09-23): every billed call takes its counts from
the response payload. Only when the payload carries none is a count
estimated, and then as exactly characters / 4 of what was actually sent
(the request body as posted) or actually received (the response content:
text, tool-call arguments, reasoning text that is billed). Estimated fields
are named, so a row never passes an estimate off as a reported count.

Claude on Bedrock reports twice: the Anthropic ``usage`` fields in the stream
(message_start for input and cache, message_delta for output, which is
CUMULATIVE per message: the last value is the count, never the sum) and
Bedrock's own ``amazon-bedrock-invocationMetrics`` trailer on message_stop
(the non-streaming invoke_model carries the same numbers as
``x-amzn-bedrock-*-token-count`` response headers). Bedrock bills from its own
metering, so the trailer is authoritative when present; when the two
disagree both are kept in ``detail`` and the disagreement is logged.

Every function returns plain count dicts shaped like
server.chat.engine.events.Usage (input_tokens, output_tokens,
cache_read_tokens, cache_write_tokens, estimated, detail). No imports from
server.chat, so both the engine and the side-task call sites can use it.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("whisper-studio")

_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

# Bedrock's metering names, per count field, in the stream trailer and in the
# invoke_model response headers.
_TRAILER_KEYS = {
    "input_tokens": "inputTokenCount",
    "output_tokens": "outputTokenCount",
    "cache_read_tokens": "cacheReadInputTokenCount",
    "cache_write_tokens": "cacheWriteInputTokenCount",
}
_HEADER_KEYS = {
    "input_tokens": "x-amzn-bedrock-input-token-count",
    "output_tokens": "x-amzn-bedrock-output-token-count",
    "cache_read_tokens": "x-amzn-bedrock-cache-read-input-token-count",
    "cache_write_tokens": "x-amzn-bedrock-cache-write-input-token-count",
}
_ANTHROPIC_KEYS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_write_tokens": "cache_creation_input_tokens",
}


def estimate_tokens(chars: int) -> int:
    """Characters / 4, rounded up so a non-empty payload never counts as 0."""
    return (max(0, int(chars)) + 3) // 4


def json_chars(obj) -> int:
    """Characters of ``obj`` serialized as a JSON request body (compact, UTF-8
    text), for callers that post a dict and never see the string."""
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str))


def counts(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    estimated: tuple[str, ...] = (),
    detail: dict | None = None,
) -> dict:
    return {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cache_read_tokens": int(cache_read_tokens or 0),
        "cache_write_tokens": int(cache_write_tokens or 0),
        "estimated": tuple(estimated),
        "detail": detail,
    }


def estimated_counts(request_chars: int, received_chars: int) -> dict:
    """No count in the payload: characters / 4 of what was posted and received."""
    return counts(
        estimate_tokens(request_chars),
        estimate_tokens(received_chars),
        estimated=("input", "output"),
    )


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _merge(anthropic: dict | None, bedrock: dict | None, *, label: str) -> dict | None:
    """The authoritative counts from the two reported sources (Bedrock's own
    metering wins field by field where it has the field), with both kept in
    ``detail`` when they disagree. None when neither reported anything."""
    if not anthropic and not bedrock:
        return None
    a = {f: int((anthropic or {}).get(f) or 0) for f in _FIELDS}
    b = dict(bedrock or {})
    chosen = {f: (b[f] if f in b else a[f]) for f in _FIELDS}
    detail = None
    if anthropic and bedrock and any(b[f] != a[f] for f in b if f in anthropic):
        detail = {"bedrock_metrics": {f: b[f] for f in b}, "anthropic_usage": a}
        log.warning(
            "%s: Bedrock metered %s but the Anthropic usage block said %s; recording "
            "Bedrock's counts and both sources",
            label,
            detail["bedrock_metrics"],
            a,
        )
    return counts(**chosen, detail=detail)


class ClaudeStreamUsage:
    """Accumulates the usage a Claude-on-Bedrock stream reports.

    ``observe`` takes every decoded stream event. message_start sets the
    prompt counts; each message_delta REPLACES the output count (and any
    input or cache count it carries: they are cumulative); message_stop
    carries Bedrock's invocationMetrics trailer."""

    def __init__(self) -> None:
        self.started = False
        self._delta_seen = False
        self._anthropic: dict[str, int] = {}
        self._bedrock: dict[str, int] | None = None

    def observe(self, data: dict) -> None:
        kind = data.get("type")
        if kind == "message_start":
            self.started = True
            self._take(data.get("message", {}).get("usage") or {})
        elif kind == "message_delta":
            self._delta_seen = True
            self._take(data.get("usage") or {})
        elif kind == "message_stop":
            metrics = data.get("amazon-bedrock-invocationMetrics")
            if isinstance(metrics, dict):
                got = {f: _int(metrics.get(k)) for f, k in _TRAILER_KEYS.items()}
                self._bedrock = {f: v for f, v in got.items() if v is not None}

    def _take(self, usage: dict) -> None:
        for field, key in _ANTHROPIC_KEYS.items():
            value = _int(usage.get(key))
            if value is not None:
                self._anthropic[field] = value

    def _output_reported(self) -> bool:
        """A real output count arrived: a message_delta (cumulative) or the
        trailer. message_start's own output_tokens is a placeholder (usually
        1), never the count."""
        return self._delta_seen or "output_tokens" in (self._bedrock or {})

    def _reported(self) -> dict:
        """The Anthropic usage fields, without message_start's placeholder
        output count when no message_delta replaced it."""
        if self._delta_seen:
            return self._anthropic
        return {f: v for f, v in self._anthropic.items() if f != "output_tokens"}

    def _with_output(self, merged: dict, received_chars: int) -> dict:
        if not self._output_reported():
            merged["output_tokens"] = estimate_tokens(received_chars)
            merged["estimated"] = ("output",)
        return merged

    def result(self, request_chars: int, received_chars: int, *, label: str) -> dict:
        """The completed round's counts: reported when the stream reported,
        characters / 4 otherwise (the whole call, or only the output when the
        stream ended with no output count)."""
        merged = _merge(self._reported(), self._bedrock, label=label)
        if merged is None:
            return estimated_counts(request_chars, received_chars)
        return self._with_output(merged, received_chars)

    def partial(self, received_chars: int, *, label: str) -> dict | None:
        """Counts for a round that ended before message_stop (a mid-stream
        error, a Stop): the prompt counts message_start reported, the output
        as characters / 4 of what arrived unless its count already came.
        None before message_start: nothing shows the prompt was ever
        processed."""
        if not self.started:
            return None
        merged = _merge(self._reported(), self._bedrock, label=label) or counts()
        return self._with_output(merged, received_chars)


def claude_invoke_counts(response: dict, payload: dict, request_body: str, *, label: str) -> dict:
    """Counts of one non-streaming Bedrock invoke_model call with an
    Anthropic body: the payload's usage, Bedrock's token-count headers, or
    characters / 4 of the body posted and the content received."""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    anthropic = None
    if isinstance(usage, dict):
        got = {f: _int(usage.get(k)) for f, k in _ANTHROPIC_KEYS.items()}
        anthropic = {f: v for f, v in got.items() if v is not None} or None
    headers = ((response or {}).get("ResponseMetadata") or {}).get("HTTPHeaders") or {}
    got = {f: _int(headers.get(k)) for f, k in _HEADER_KEYS.items()}
    bedrock = {f: v for f, v in got.items() if v is not None} or None
    merged = _merge(anthropic, bedrock, label=label)
    if merged:
        return merged
    return estimated_counts(len(request_body or ""), _anthropic_content_chars(payload))


def input_billed_counts(response: dict, payload: dict, request_body: str) -> dict:
    """Counts of one invoke_model call to a model that returns no generated
    tokens (Cohere embed and rerank): Bedrock's input token-count header, the
    payload's own ``meta.billed_units.input_tokens``, else characters / 4 of
    the body posted. The output is zero: vectors and scores are not tokens."""
    headers = ((response or {}).get("ResponseMetadata") or {}).get("HTTPHeaders") or {}
    reported = _int(headers.get(_HEADER_KEYS["input_tokens"]))
    if reported is None and isinstance(payload, dict):
        billed = (payload.get("meta") or {}).get("billed_units") or {}
        reported = _int(billed.get("input_tokens")) if isinstance(billed, dict) else None
    if reported is not None:
        return counts(reported, 0)
    return counts(estimate_tokens(len(request_body or "")), 0, estimated=("input",))


def _anthropic_content_chars(payload) -> int:
    total = 0
    for block in (payload or {}).get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            total += len(block.get("text") or "")
        elif block.get("type") == "thinking":
            total += len(block.get("thinking") or "")
        elif block.get("type") == "tool_use":
            total += json_chars(block.get("input") or {})
    return total


def _cache_write_count(usage, details) -> int:
    """Mantle's prompt-cache write count. The OpenAI SDK declares no such
    field, but mantle adds ``cache_write_tokens`` to the usage (a live probe
    saw it on GPT-5.6 and GPT-6 with no explicit cache key, and Cost Explorer
    bills those tokens as 30-minute cache writes). It is read next to
    cached_tokens in input_tokens_details, or on the usage itself; the SDK
    keeps undeclared keys as attributes."""
    for holder in (details, usage):
        value = _int(getattr(holder, "cache_write_tokens", None))
        if value is not None:
            return value
    return 0


def responses_counts(usage, request_chars: int, received_chars: int) -> dict:
    """Counts of one OpenAI Responses call on bedrock-mantle: ``usage``
    (input_tokens, output_tokens, input_tokens_details.cached_tokens and
    mantle's cache_write_tokens, both subsets of input) when the response
    carried it, characters / 4 otherwise."""
    if usage is not None:
        inp = _int(getattr(usage, "input_tokens", None))
        out = _int(getattr(usage, "output_tokens", None))
        if inp is not None or out is not None:
            details = getattr(usage, "input_tokens_details", None)
            cached = _int(getattr(details, "cached_tokens", None)) or 0
            return counts(inp or 0, out or 0, cached, _cache_write_count(usage, details))
    return estimated_counts(request_chars, received_chars)
