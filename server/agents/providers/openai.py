"""OpenAI-on-Bedrock adapter (Responses API via bedrock-mantle).

Converts the canonical Anthropic message shape to Responses input items and
back. The wire call streams and is consumed INTERNALLY (no SSE): bedrock-
mantle holds a non-streamed response until the whole answer is generated,
which would add a long tail to every agent turn; streaming delivers events
as they happen and ends naturally at ``response.completed``, which also
carries the usage.
"""

from __future__ import annotations

import asyncio
import json
import logging

from server.agents.providers.base import (
    AGENT_CALL_CONCURRENCY,
    CountsHook,
    ProviderTurn,
    TurnUsage,
    structured_from_text,
)

log = logging.getLogger("whisper-studio")

_semaphore: asyncio.Semaphore | None = None


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(AGENT_CALL_CONCURRENCY)
    return _semaphore


def canonical_to_responses_items(messages: list[dict]) -> list[dict]:
    """Canonical Anthropic-shaped history -> Responses input items.

    assistant tool_use -> function_call (call_id = block id);
    user tool_result -> function_call_output; thinking blocks skipped;
    plain text flows through as message items.
    """
    from server.openai_bedrock.runtime import tool_result_input_items

    items: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")
        if isinstance(content, str):
            if content:
                items.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use" and role == "assistant":
                if text_parts:
                    items.append({"role": role, "content": "\n\n".join(text_parts)})
                    text_parts = []
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}),
                    }
                )
            elif btype == "tool_result" and role == "user":
                if text_parts:
                    items.append({"role": role, "content": "\n\n".join(text_parts)})
                    text_parts = []
                items.extend(
                    tool_result_input_items(block.get("tool_use_id", ""), block.get("content"))
                )
            # thinking / redacted_thinking: provider-specific, skipped here
        if text_parts:
            items.append({"role": role, "content": "\n\n".join(text_parts)})
    return items


class OpenAIBedrockAdapter:
    def __init__(self, *, model_key: str, model_id: str):
        self.model_key = model_key
        self.model_id = model_id

    async def invoke(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict] | None,
        max_tokens: int,
        effort_label: str | None = None,
        force_structured: dict | None = None,
        on_counts: CountsHook | None = None,
    ) -> ProviderTurn:
        from server.costs.capture import json_chars, responses_counts
        from server.openai_bedrock import runtime as oai

        region = oai.region_for(self.model_key)
        client = oai.build_client(region)
        instructions = system + oai.GPT_INSTRUCTIONS_SUFFIX
        input_items = canonical_to_responses_items(messages)

        kwargs: dict = {
            "model": self.model_id,
            "instructions": instructions,
            "input": input_items,
            "max_output_tokens": max_tokens,
            "store": False,
            "stream": True,
        }
        effort = oai.reasoning_effort_for(self.model_key, effort_label)
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        if force_structured is not None:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "result",
                    "schema": force_structured,
                    "strict": False,
                }
            }
        elif tools:
            translated = oai.translate_tools(
                [t for t in tools if t.get("name") not in oai.EXCLUDED_TOOLS]
            )
            if translated:
                kwargs["tools"] = translated

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        stop_reason = "end_turn"
        # The call's counts: response.completed's usage (or a failed
        # response's), else characters / 4 of the posted request and of the
        # billed content received (text, raw reasoning, tool arguments).
        # Reported once the stream has delivered an event, whether it
        # completes, ends early or breaks.
        request_chars = json_chars(kwargs)
        meter = {"events": 0, "usage": None, "received": 0, "args": {}}
        counts = None

        def _counts() -> dict:
            received = meter["received"] + sum(meter["args"].values())
            return responses_counts(meter["usage"], request_chars, received)

        # call_id -> {name, args_json}
        pending: dict[str, dict] = {}
        order: list[str] = []
        try:
            async with _sem():
                stream = await client.responses.create(**kwargs)
                async for event in stream:
                    meter["events"] += 1
                    et = getattr(event, "type", "")
                    if et == "response.output_text.delta":
                        d = getattr(event, "delta", "") or ""
                        meter["received"] += len(d)
                        text_parts.append(d)
                    elif et == "response.reasoning_text.delta":
                        meter["received"] += len(getattr(event, "delta", "") or "")
                    elif et == "response.function_call_arguments.delta":
                        key = getattr(event, "item_id", "") or ""
                        d = getattr(event, "delta", "") or ""
                        meter["args"][key] = meter["args"].get(key, 0) + len(d)
                    elif et == "response.output_item.done":
                        item = getattr(event, "item", None)
                        if item is not None and getattr(item, "type", "") == "function_call":
                            call_id = getattr(item, "call_id", "") or getattr(item, "id", "")
                            if call_id and call_id not in pending:
                                order.append(call_id)
                            arguments = getattr(item, "arguments", "") or "{}"
                            meter["args"][getattr(item, "id", "") or call_id] = len(arguments)
                            pending[call_id] = {
                                "name": getattr(item, "name", ""),
                                "arguments": arguments,
                            }
                    elif et in ("response.completed", "response.incomplete", "response.failed"):
                        # Each ends the response with the usage it billed.
                        resp = getattr(event, "response", None)
                        meter["usage"] = getattr(resp, "usage", None)
            counts = _counts()
        finally:
            if on_counts is not None and meter["events"]:
                on_counts(counts or _counts())
        usage = TurnUsage.from_counts(counts, self.model_key)

        assistant_blocks: list[dict] = []
        text = "".join(text_parts)
        if text:
            assistant_blocks.append({"type": "text", "text": text})
        for call_id in order:
            call = pending[call_id]
            try:
                parsed_input = json.loads(call["arguments"]) if call["arguments"] else {}
            except json.JSONDecodeError:
                parsed_input = {}
            block = {
                "type": "tool_use",
                "id": call_id,
                "name": call["name"],
                "input": parsed_input,
            }
            tool_calls.append(block)
            assistant_blocks.append(block)
        if tool_calls:
            stop_reason = "tool_use"

        structured = structured_from_text(text) if force_structured is not None else None

        return ProviderTurn(
            text=text,
            tool_calls=tool_calls,
            assistant_blocks=assistant_blocks,
            stop_reason=stop_reason,
            usage=usage,
            structured_output=structured,
        )
