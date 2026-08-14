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
    RoundError,
    RoundResult,
    TextDelta,
    ThinkingDelta,
    ThinkingStart,
    ThinkingStop,
    ToolCall,
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
        # Per-model output cap: chat_model_meta.max_output when configured,
        # else the Claude-on-Bedrock default.
        from server.infrastructure.config import load_config

        meta = (load_config().get("chat_model_meta") or {}).get(model_key) or {}
        # Kept for the effort mapping too: "ultracode" has no fixed wire value,
        # it rides this model's own top reasoning rung.
        self._meta = meta
        try:
            self.max_tokens = int(meta.get("max_output") or 128000)
        except (TypeError, ValueError):
            self.max_tokens = 128000

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
        if self.effort_label is not None:
            body["thinking"] = {"type": "adaptive"}
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

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        """Yield neutral RoundEvents for one model round. Raises
        PromptTooLongError / WhisperAPIError at invoke time (engine handles)."""

        def _call(messages=messages):
            return self._client.invoke_model_with_response_stream(
                modelId=self.model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(
                    self._build_body(messages, tools, core_count, round_num, is_last_round)
                ),
            )

        response = await invoke_with_retry(
            call_fn=_call,
            loop=self._loop,
            executor=self._executor,
        )

        q: asyncio.Queue = asyncio.Queue()
        stop_reading = threading.Event()

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
                                q.put_nowait(
                                    classify_bedrock_error(Exception(f"{canonical}: {msg}"))
                                )
                                return
                        continue
                    q.put_nowait(json.loads(chunk["bytes"].decode("utf-8")))
                if not stop_reading.is_set():
                    q.put_nowait(None)
            except Exception as e:
                if not stop_reading.is_set():
                    q.put_nowait(e)

        self._loop.run_in_executor(self._executor, _read_stream)

        content_blocks: list[dict] = []
        current_block: dict | None = None
        stop_reason = "end_turn"
        in_tok = out_tok = cache_read = cache_creation = 0

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
                    yield RoundError(message=api_err.user_message)
                    return
                event_type = data.get("type")

                if event_type == "message_start":
                    usage = data.get("message", {}).get("usage", {})
                    in_tok += usage.get("input_tokens", 0)
                    cache_read += usage.get("cache_read_input_tokens", 0)
                    cache_creation += usage.get("cache_creation_input_tokens", 0)

                elif event_type == "content_block_start":
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
                        yield TextDelta(text=text)
                    elif (
                        delta.get("type") == "input_json_delta"
                        and current_block
                        and current_block["type"] == "tool_use"
                    ):
                        current_block["input_json"] += delta.get("partial_json", "")

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
                    out_tok += data.get("usage", {}).get("output_tokens", 0)
        finally:
            stop_reading.set()
            try:
                body_stream = response.get("body")
                if body_stream is not None:
                    body_stream.close()
            except Exception:
                pass

        # Cache canary: caching on but nothing read from cache by round 3+.
        if self.caching_on and round_num >= 2 and cache_read == 0 and not _CACHE_CANARY["fired"]:
            _CACHE_CANARY["fired"] = True
            log.warning(
                "prompt_caching is ON but cache_read is still 0 after %d rounds — "
                "checkpoints may be misplaced or the cache prefix is churning "
                "(tool pool / system prompt instability)",
                round_num + 1,
            )

        yield RoundResult(
            stop_reason=stop_reason,
            content=_to_canonical_content(content_blocks),
            usage=Usage(
                input_tokens=in_tok,
                output_tokens=out_tok,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_creation,
            ),
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
