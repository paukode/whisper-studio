"""Anthropic-on-Bedrock adapter: the runtime's original invoke_model path,
plus what it always should have had — effort/adaptive thinking, usage
extraction, redacted_thinking preservation, and structured forcing.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from server.agents.providers.base import AGENT_CALL_CONCURRENCY, ProviderTurn, TurnUsage

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
    ) -> ProviderTurn:
        body: dict = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if force_structured is not None:
            # Swap tools for a single schema-shaped emit_result tool and force
            # it. Pinned Bedrock constraint: forced tool_choice combined with
            # extended/adaptive thinking raises ValidationException — omit
            # thinking on this one call.
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
            body["tool_choice"] = {"type": "tool", "name": STRUCTURED_TOOL_NAME}
        else:
            if tools:
                body["tools"] = tools
            if effort_label is not None:
                from server.infrastructure.effort import api_effort

                body["thinking"] = {"type": "adaptive"}
                body["output_config"] = {"effort": api_effort(effort_label)}

        bedrock = self._bedrock_client()

        def _invoke(b=body):
            resp = bedrock.invoke_model(
                modelId=self.model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(b),
            )
            return json.loads(resp["body"].read())

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(_executor, _invoke)

        raw_usage = response.get("usage", {}) or {}
        usage = TurnUsage(
            input_tokens=raw_usage.get("input_tokens", 0),
            output_tokens=raw_usage.get("output_tokens", 0),
            cache_read_tokens=raw_usage.get("cache_read_input_tokens", 0),
            cache_creation_tokens=raw_usage.get("cache_creation_input_tokens", 0),
        )

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

        return ProviderTurn(
            text="\n\n".join(t for t in text_parts if t),
            tool_calls=tool_calls,
            assistant_blocks=assistant_blocks,
            stop_reason=response.get("stop_reason", "end_turn"),
            usage=usage,
            structured_output=structured,
        )
