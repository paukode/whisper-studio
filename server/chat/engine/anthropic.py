"""Anthropic-on-Bedrock adapter: wire protocol in, neutral round events out.

Owns everything Bedrock/Claude-specific — request body shape, prompt-cache
checkpoints (tools/system/moving message breakpoints and the canary), the
invoke retry wrapper, the threaded EventStream reader, and the decode of
Anthropic stream events into the neutral vocabulary in ``events``.

``stream_round`` raises ``PromptTooLongError`` at invoke time (the engine owns
the rescue) and yields a terminal ``RoundResult`` carrying the canonical
assistant content on success. Mid-stream failures become ``RoundError``.
"""

import asyncio
import json
import logging
import threading

from server.chat.infra import _get_bedrock_client
from server.infrastructure.bedrock_retry import invoke_with_retry
from server.infrastructure.errors import (
    WhisperAPIError,
    classify_bedrock_error,
)

from .events import (
    TOOL_ARGS_PROGRESS_STEP,
    RoundError,
    RoundResult,
    TextDelta,
    ThinkingDelta,
    ThinkingStart,
    ThinkingStop,
    ToolCall,
    ToolCallProgress,
    ToolCallStart,
    Usage,
)

log = logging.getLogger("whisper-studio")

# One-shot per process: prompt caching is on but nothing ever reads from the
# cache — the breakpoints are misplaced or the prefix churns.
_CACHE_CANARY = {"fired": False}

# Bedrock surfaces mid-stream failures as non-chunk events keyed by the
# exception type (lowercase first letter). Map to the canonical name so
# classify_bedrock_error routes them correctly.
_STREAM_ERROR_KEYS = {
    "internalServerException": "InternalServerException",
    "modelStreamErrorException": "ModelStreamErrorException",
    "validationException": "ValidationException",
    "throttlingException": "ThrottlingException",
    "serviceUnavailableException": "ServiceUnavailableException",
}


class AnthropicAdapter:
    provider = "anthropic"

    def __init__(
        self,
        *,
        model_key: str,
        model_id: str,
        system_prompt,
        system_static: str,
        system_dynamic: str,
        caching_on: bool,
        cache_ttl: str,
        effort_label: str | None,
        force_skill: str | None,
        loop,
        executor,
        meta: dict | None = None,
    ):
        self.model_key = model_key
        self.model_id = model_id
        self.system_prompt = system_prompt
        self.system_static = system_static
        self.system_dynamic = system_dynamic
        self.caching_on = caching_on
        self.cache_ttl = cache_ttl
        self.effort_label = effort_label
        self.force_skill = force_skill
        self._loop = loop
        self._executor = executor
        self._client = _get_bedrock_client()
        # The round in flight, for inflight_usage: (meter, label) and the
        # characters of content received so far.
        self._inflight = None
        self._inflight_received = 0
        # Per-model metadata, driving the output cap (chat_model_meta.max_output)
        # and the effort mapping ("ultracode" has no fixed wire value; it rides
        # this model's own top reasoning rung).
        #
        # The caller passes the metadata it resolved the effort FROM, which for a
        # chat turn is the workspace-aware latched snapshot. Re-reading the global
        # config here would disagree with it for a model defined only in a
        # workspace's .whisper/settings.json: the label would say ultracode and
        # the wire value would come from a config entry that does not exist.
        # The global read stays as the fallback for callers with no metadata.
        if meta is None:
            from server.infrastructure.config import load_config

            meta = (load_config().get("chat_model_meta") or {}).get(model_key) or {}
        self._meta = meta
        # Bedrock rejects max_tokens above the model's output ceiling (Haiku
        # 64K, other current tiers 128K — see windows.anthropic_output_ceiling,
        # which the context budgeting mirrors). A configured max_output wins
        # below the ceiling; at or above it we clamp instead of letting the
        # API error kill the turn.
        from server.chat.engine.windows import anthropic_output_ceiling

        ceiling = anthropic_output_ceiling(model_id)
        try:
            self.max_tokens = min(int(meta.get("max_output") or ceiling), ceiling)
        except (TypeError, ValueError):
            self.max_tokens = ceiling

    def describe_request(self) -> dict:
        """The provider-specific half of a request snapshot — everything this
        adapter contributes to the wire request that the engine cannot see
        (server/chat/request_snapshots.py)."""
        return {
            "system_static": self.system_static,
            "system_dynamic": self.system_dynamic,
            "system_prompt": "" if self.caching_on else self.system_prompt,
            "caching_on": self.caching_on,
            "cache_ttl": self.cache_ttl,
            "effort_label": self.effort_label,
            "force_skill": self.force_skill,
            "max_tokens": getattr(self, "max_tokens", None),
        }

    # ── Request building (port of call_bedrock_stream) ───────────────────────

    def _build_body(self, messages, tools, core_count, round_num, is_last_round) -> dict:
        from server.infrastructure.effort import api_effort_for

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_tokens,
            "system": self.system_prompt,
            "messages": messages,
        }
        # Effort → adaptive thinking + output_config.effort. Models with no
        # effort tier (Haiku) send neither.
        #
        # ``display`` is what puts text in the thinking blocks. It defaults to
        # "omitted" on every current model (Opus 4.7+, Sonnet 5, Fable), which
        # still streams a thinking block — it just carries an empty string. That
        # reads in the UI as a "Thinking…" header that never fills in, because
        # ThinkingStart fires on content_block_start while no ThinkingDelta ever
        # carries text. Asking for "summarized" is what Opus 4.6 and Sonnet 4.6
        # did by default, and it does not change what is thought or billed —
        # only whether the summary comes back on the wire.
        if self.effort_label is not None:
            body["thinking"] = {"type": "adaptive", "display": "summarized"}
            body["output_config"] = {
                "effort": api_effort_for(self.effort_label, self._meta, self.model_key)
            }
        if not is_last_round:
            # Prompt caching: checkpoint on the LAST tool and the STATIC system
            # block; third moving checkpoint on the last message. Only when
            # tools are present (the static block alone is below the 4096-token
            # cacheable minimum). Truthy check: an empty static block must not
            # be cached.
            if self.caching_on and tools and self.system_static:
                from server.chat.caching import annotate_messages_cache, cached_tools_and_system

                body["tools"], body["system"] = cached_tools_and_system(
                    tools,
                    self.system_static,
                    self.system_dynamic,
                    self.cache_ttl,
                    core_count=core_count
                    if core_count is not None and core_count < len(tools)
                    else None,
                )
                body["messages"] = annotate_messages_cache(messages, self.cache_ttl)
            else:
                body["tools"] = tools
            # Force the requested skill ONLY on round 0, and only when thinking
            # is off — Bedrock rejects forced tool_choice combined with
            # adaptive/extended thinking.
            if (
                self.force_skill
                and round_num == 0
                and "thinking" not in body
                and any(t.get("name") == self.force_skill for t in tools)
            ):
                body["tool_choice"] = {"type": "tool", "name": self.force_skill}
        return body

    # ── One streamed round ────────────────────────────────────────────────────

    def inflight_usage(self) -> Usage | None:
        """What the round in flight has cost so far, for the engine to record
        when the round ends without a RoundResult (a mid-stream error, a
        retry, a Stop): the prompt counts message_start reported plus the
        output as characters / 4 of what arrived. None when no round is in
        flight or the provider never started one (nothing was billed)."""
        inflight = getattr(self, "_inflight", None)
        if inflight is None:
            return None
        meter, label = inflight
        partial = meter.partial(self._inflight_received, label=label)
        return Usage(**partial) if partial else None

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        """Yield neutral RoundEvents for one model round. Raises
        PromptTooLongError / WhisperAPIError at invoke time (engine handles)."""
        from server.costs.capture import ClaudeStreamUsage

        meter = ClaudeStreamUsage()
        label = f"{self.model_key} round {round_num}"
        self._inflight = (meter, label)
        self._inflight_received = 0
        # Characters of the exact body posted: the input estimate should the
        # stream report no usage at all. Built in the invoke worker, as before.
        posted = {"chars": 0}

        def _call(messages=messages):
            body = json.dumps(
                self._build_body(messages, tools, core_count, round_num, is_last_round)
            )
            posted["chars"] = len(body)
            return self._client.invoke_model_with_response_stream(
                modelId=self.model_id,
                contentType="application/json",
                accept="application/json",
                body=body,
            )

        response = await invoke_with_retry(
            call_fn=_call,
            loop=self._loop,
            executor=self._executor,
        )

        q: asyncio.Queue = asyncio.Queue()
        stop_reading = threading.Event()
        loop = asyncio.get_running_loop()  # the loop that owns ``q``

        def _post(item) -> None:
            # asyncio.Queue is not thread-safe: a put_nowait from this worker
            # resolves the waiting getter without waking the loop, so each
            # chunk sat until some unrelated wakeup (and under uvloop it
            # touches libuv state off the loop thread). Hand it over through
            # the loop instead. A closed loop (shutdown mid-stream) has nobody
            # left to read, so the item is dropped.
            try:
                loop.call_soon_threadsafe(q.put_nowait, item)
            except RuntimeError:
                stop_reading.set()

        def _read_stream():
            try:
                stream = response.get("body")
                for event in stream:
                    if stop_reading.is_set():
                        return
                    chunk = event.get("chunk")
                    if not chunk:
                        for ekey, canonical in _STREAM_ERROR_KEYS.items():
                            if ekey in event:
                                msg = (event[ekey] or {}).get("message", canonical)
                                _post(classify_bedrock_error(Exception(f"{canonical}: {msg}")))
                                return
                        continue
                    _post(json.loads(chunk["bytes"].decode("utf-8")))
                if not stop_reading.is_set():
                    _post(None)
            except Exception as e:
                if not stop_reading.is_set():
                    _post(e)

        self._loop.run_in_executor(self._executor, _read_stream)

        content_blocks: list[dict] = []
        current_block: dict | None = None
        stop_reason = "end_turn"

        try:
            while True:
                data = await q.get()
                if data is None:
                    break
                if isinstance(data, Exception):
                    api_err = (
                        data if isinstance(data, WhisperAPIError) else classify_bedrock_error(data)
                    )
                    log.warning("Bedrock stream error: %s", api_err)
                    yield RoundError(message=api_err.user_message, retryable=api_err.is_retryable)
                    return
                event_type = data.get("type")
                # Usage: message_start (prompt), message_delta (cumulative
                # output, the last value is the count), message_stop (Bedrock's
                # invocationMetrics trailer).
                meter.observe(data)

                if event_type == "content_block_start":
                    block = data.get("content_block", {})
                    current_block = {
                        "type": block.get("type"),
                        "text": "",
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input_json": "",
                        "signature": "",
                    }
                    content_blocks.append(current_block)
                    if current_block["type"] == "tool_use":
                        yield ToolCallStart(name=current_block["name"])
                    elif current_block["type"] == "thinking":
                        yield ThinkingStart()

                elif event_type == "content_block_delta":
                    delta = data.get("delta", {})
                    if (
                        delta.get("type") == "thinking_delta"
                        and current_block
                        and current_block["type"] == "thinking"
                    ):
                        text = delta.get("thinking", "")
                        current_block["text"] += text
                        self._inflight_received += len(text)
                        yield ThinkingDelta(text=text)
                    elif (
                        delta.get("type") == "signature_delta"
                        and current_block
                        and current_block["type"] == "thinking"
                    ):
                        current_block["signature"] += delta.get("signature", "")
                    elif (
                        delta.get("type") == "text_delta"
                        and current_block
                        and current_block["type"] == "text"
                    ):
                        text = delta.get("text", "")
                        current_block["text"] += text
                        self._inflight_received += len(text)
                        yield TextDelta(text=text)
                    elif (
                        delta.get("type") == "input_json_delta"
                        and current_block
                        and current_block["type"] == "tool_use"
                    ):
                        piece = delta.get("partial_json", "")
                        current_block["input_json"] += piece
                        self._inflight_received += len(piece)
                        # A large tool argument (a whole HTML app) streams for
                        # minutes with nothing else to show: report its size.
                        _n = len(current_block["input_json"])
                        if _n >= current_block.get("next_progress", TOOL_ARGS_PROGRESS_STEP):
                            current_block["next_progress"] = (
                                _n // TOOL_ARGS_PROGRESS_STEP + 1
                            ) * TOOL_ARGS_PROGRESS_STEP
                            yield ToolCallProgress(name=current_block["name"], chars=_n)

                elif event_type == "content_block_stop":
                    if current_block and current_block["type"] == "thinking":
                        yield ThinkingStop()
                    elif current_block and current_block["type"] == "tool_use":
                        try:
                            parsed_input = (
                                json.loads(current_block["input_json"])
                                if current_block["input_json"]
                                else {}
                            )
                        except json.JSONDecodeError:
                            parsed_input = {}
                        current_block["parsed_input"] = parsed_input
                        yield ToolCall(
                            id=current_block["id"],
                            name=current_block["name"],
                            input=parsed_input,
                        )

                elif event_type == "message_delta":
                    stop_reason = data.get("delta", {}).get("stop_reason", stop_reason)
        finally:
            stop_reading.set()
            try:
                body_stream = response.get("body")
                if body_stream is not None:
                    body_stream.close()
            except Exception:
                pass

        usage = Usage(**meter.result(posted["chars"], self._inflight_received, label=label))
        # Cache canary: caching on but nothing read from cache by round 3+.
        if (
            self.caching_on
            and round_num >= 2
            and usage.cache_read_tokens == 0
            and not _CACHE_CANARY["fired"]
        ):
            _CACHE_CANARY["fired"] = True
            log.warning(
                "prompt_caching is ON but cache_read is still 0 after %d rounds — "
                "checkpoints may be misplaced or the cache prefix is churning "
                "(tool pool / system prompt instability)",
                round_num + 1,
            )

        # The round completed: its cost travels on the RoundResult, so the
        # engine must not also record it as an unfinished attempt.
        self._inflight = None
        yield RoundResult(
            stop_reason=stop_reason,
            content=_to_canonical_content(content_blocks),
            usage=usage,
        )


def _to_canonical_content(content_blocks: list[dict]) -> list[dict]:
    """Streamed block accumulators → canonical assistant content blocks."""
    result = []
    for b in content_blocks:
        if b["type"] == "thinking":
            result.append(
                {"type": "thinking", "thinking": b["text"], "signature": b.get("signature", "")}
            )
        elif b["type"] == "text":
            result.append({"type": "text", "text": b["text"]})
        elif b["type"] == "tool_use":
            result.append(
                {
                    "type": "tool_use",
                    "id": b["id"],
                    "name": b["name"],
                    "input": b.get("parsed_input", {}),
                }
            )
    return result
