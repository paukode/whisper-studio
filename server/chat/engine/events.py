"""Neutral round events — the vocabulary every provider stream reduces to.

A provider adapter consumes its wire protocol (Bedrock invoke stream, OpenAI
Responses events, llama-server SSE) and yields these events; the turn engine
consumes them without knowing which provider produced them. Adapters do NOT
interpret — classification of errors (is this retryable? is the prompt too
long, and what is the cap?) happens at the adapter boundary so the engine's
handling is uniform.

Pure data: no behavior lives here, and nothing here may import provider code.
"""

from dataclasses import dataclass, field
from enum import Enum


class ErrorKind(Enum):
    """How the engine should react to a round failure."""

    PROMPT_TOO_LONG = "prompt_too_long"  # trim context and retry the round
    AUTH = "auth"  # fatal: surface guidance, end the turn
    THROTTLED = "throttled"  # adapter exhausted its own retries; fatal
    TRANSIENT = "transient"  # network/stream hiccup; fatal after adapter retries
    FATAL = "fatal"  # everything else


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
    kind: ErrorKind
    message: str
    # PROMPT_TOO_LONG only: the model's input cap parsed from the provider
    # error, so the engine can budget the retry against the real limit even
    # when config carries no context_window for the model.
    prompt_cap_tokens: int | None = None


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
