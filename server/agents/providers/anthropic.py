"""Anthropic-on-Bedrock adapter: the runtime's original invoke_model path,
plus what it always should have had: effort/adaptive thinking, token counts
through server.costs.capture, redacted_thinking preservation, and the
structured-output call.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from server.agents.providers.base import (
    AGENT_CALL_CONCURRENCY,
    CountsHook,
    ProviderTurn,
    TurnUsage,
    structured_from_text,
)

log = logging.getLogger("whisper-studio")

# One shared throttle for every Anthropic agent call in the process.
_executor = ThreadPoolExecutor(max_workers=AGENT_CALL_CONCURRENCY)

STRUCTURED_TOOL_NAME = "emit_result"


class AnthropicBedrockAdapter:
    def __init__(self, *, model_key: str, model_id: str):
        self.model_key = model_key
        self.model_id = model_id
        self._bedrock = None

    def _bedrock_client(self):
        # The chat module owns the ONE bedrock-runtime client construction
        # path (region/config/retries); reusing it keeps a single seam for
        # configuration and for the test suite's client fakes.
        if self._bedrock is None:
            from server.chat import _get_bedrock_client

            self._bedrock = _get_bedrock_client()
        return self._bedrock

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
        body: dict = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if force_structured is not None:
            # Swap tools for a single schema-shaped emit_result tool. The model
            # decides whether to call it (tool_choice auto): Opus 5.5, Sonnet
            # 5.5, Fable 5.1 and Mythos 5.1 reject a forced tool_choice with a
            # 400. The caller's ask names the tool or a JSON answer (read from
            # the text below) and retries once when neither comes back.
            # Thinking stays unset, which every catalog model accepts: Fable,
            # Opus 5.x and Sonnet 5.x then think adaptively, the others do not.
            body["tools"] = [
                {
                    "name": STRUCTURED_TOOL_NAME,
                    "description": (
                        "Emit the final structured result. Call exactly once "
                        "with the complete result object."
                    ),
                    "input_schema": force_structured,
                }
            ]
            body["tool_choice"] = {"type": "auto"}
        else:
            if tools:
                body["tools"] = tools
            if effort_label is not None:
                from server.chat.infra import _get_chat_model_meta
                from server.infrastructure.effort import api_effort_for

                meta = _get_chat_model_meta().get(self.model_key, {})
                body["thinking"] = {"type": "adaptive"}
                body["output_config"] = {
                    "effort": api_effort_for(effort_label, meta, self.model_key)
                }

        bedrock = self._bedrock_client()
        request_body = json.dumps(body)
        label = f"agent call ({self.model_key or self.model_id})"

        def _invoke():
            from server.costs.capture import claude_invoke_counts

            resp = bedrock.invoke_model(
                modelId=self.model_id,
                contentType="application/json",
                accept="application/json",
                body=request_body,
            )
            # The call returned, so it was billed: its counts come from the
            # payload's usage, Bedrock's token-count headers, or characters /
            # 4 of what was posted and received, even when the body is
            # unreadable.
            try:
                payload = json.loads(resp["body"].read())
            except Exception:
                if on_counts is not None:
                    on_counts(claude_invoke_counts(resp, {}, request_body, label=label))
                raise
            counts = claude_invoke_counts(resp, payload, request_body, label=label)
            if on_counts is not None:
                on_counts(counts)
            return payload, counts

        loop = asyncio.get_running_loop()
        response, counts = await loop.run_in_executor(_executor, _invoke)
        usage = TurnUsage.from_counts(counts, self.model_key)

        content_blocks = response.get("content", []) or []
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        assistant_blocks: list[dict] = []
        structured: dict | None = None
        for block in content_blocks:
            btype = block.get("type", "")
            if btype == "text":
                text_parts.append(block.get("text", ""))
                assistant_blocks.append(block)
            elif btype == "tool_use":
                if force_structured is not None and block.get("name") == STRUCTURED_TOOL_NAME:
                    structured = copy.deepcopy(block.get("input") or {})
                    assistant_blocks.append(block)
                else:
                    tool_calls.append(block)
                    assistant_blocks.append(block)
            elif btype in ("thinking", "redacted_thinking"):
                # BOTH must survive into history: replaying a multi-turn
                # conversation with missing thinking blocks is rejected once
                # adaptive thinking is enabled.
                assistant_blocks.append(block)
        text = "\n\n".join(t for t in text_parts if t)
        if force_structured is not None and structured is None:
            # Nothing forces the emit_result call, and the ask also allows the
            # required JSON format: an object answered in text is the result.
            structured = structured_from_text(text)

        return ProviderTurn(
            text=text,
            tool_calls=tool_calls,
            assistant_blocks=assistant_blocks,
            stop_reason=response.get("stop_reason", "end_turn"),
            usage=usage,
            structured_output=structured,
        )
