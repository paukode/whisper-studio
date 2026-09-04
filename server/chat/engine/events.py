"""Neutral round events — the vocabulary every provider stream reduces to.

A provider adapter consumes its wire protocol (Bedrock invoke stream, OpenAI
Responses events, llama-server SSE) and yields these events; the turn engine
consumes them without knowing which provider produced them. Pre-stream
failures are retried inside the adapters/SDKs (retry helpers,
PromptTooLongError raised for the engine's reactive-compaction path). A
RoundError marked ``retryable`` is a transient MID-STREAM failure the SDKs
cannot retry (the HTTP response was already 200) — the engine may re-run the
round; any other RoundError is terminal for the turn.

Pure data: no behavior lives here, and nothing here may import provider code.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ThinkingStart:
    pass


@dataclass(frozen=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True)
class ThinkingStop:
    pass


@dataclass(frozen=True)
class ToolCallStart:
    """A tool call has begun streaming (arguments not yet complete). The UI
    shows the running tool immediately; the complete call follows."""

    name: str


@dataclass(frozen=True)
class ToolCall:
    """One complete tool call (adapters buffer argument deltas themselves —
    the engine never sees partial JSON)."""

    id: str
    name: str
    input: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Usage:
    """Token truth for the round, when the provider reports it. ``exact`` is
    False when the adapter estimated (e.g. mantle's early release skips the
    usage-bearing completed event)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    exact: bool = True


@dataclass(frozen=True)
class RoundError:
    """Round failure. ``retryable`` marks a transient provider fault that
    struck mid-stream (server_error/5xx/throttle after HTTP 200 — the SDK
    retry layer never sees these): the engine may re-run the round instead
    of ending the turn. Non-retryable errors end the turn with the message."""

    message: str
    retryable: bool = False


@dataclass(frozen=True)
class Incomplete:
    """The provider truncated output (output-token cap), not an error."""

    reason: str = "max_output_tokens"


@dataclass(frozen=True)
class Heartbeat:
    """Idle-gap keepalive; the SSE sink forwards it as a comment frame."""

    pass


@dataclass(frozen=True)
class RoundResult:
    """Terminal event of a successful round: the provider-canonical assistant
    content (Anthropic block shapes — text/thinking/tool_use), the stop
    reason, and the round's token usage. Always the LAST event an adapter
    yields for a round; the engine appends ``content`` to the conversation."""

    stop_reason: str
    content: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


RoundEvent = (
    TextDelta
    | ThinkingStart
    | ThinkingDelta
    | ThinkingStop
    | ToolCallStart
    | ToolCall
    | Usage
    | RoundError
    | Incomplete
    | Heartbeat
    | RoundResult
)
