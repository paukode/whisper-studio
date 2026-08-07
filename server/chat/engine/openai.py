"""OpenAI-on-Bedrock (Responses API) adapter: mantle wire protocol in, neutral
round events out.

Owns everything GPT/mantle-specific:

  - canonical messages → Responses ``input`` items with full function-call
    fidelity (assistant ``tool_use`` blocks become ``function_call`` items and
    tool_result user messages become ``function_call_output`` items, so the
    call linkage survives round-tripping through the engine's canonical
    format — the old path degraded history tool calls to plain text)
  - the two mantle stream adaptations, verified live: EARLY RELEASE (mantle
    holds the stream open ~30-40s after the answer before ``completed``) and
    HEARTBEAT keepalives during idle gaps
  - prompt-too-long detection: mantle rejections like ``prompt tokens (281096)
    exceed customer model maximum (278528)`` raise ``PromptTooLongError`` so
    the engine's reactive rescue + salvage round apply to GPT turns exactly as
    they do to Claude
  - effort/verbosity mapping and the tool exclusions (EXCLUDED_TOOLS)

Cache note: the Responses API has no explicit checkpoints; a stable
``prompt_cache_key`` per session plus deterministic input conversion gives
prefix reuse. On the last round tools stay in the request with
``tool_choice="none"`` so the cached prefix holds.
"""

import asyncio
import json
import logging

from server.infrastructure.errors import PromptTooLongError

from . import windows
from .events import (
    Heartbeat,
    Incomplete,
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

_POLL_S = 0.25
_HEARTBEAT_S = 5.0
_EARLY_RELEASE_GRACE_S = 1.0


def _is_prompt_too_long(message: str) -> bool:
    low = (message or "").lower()
    return (
        windows.parse_prompt_cap(message) is not None
        or "too long" in low
        or ("prompt tokens" in low and "exceed" in low)
        or "context_length_exceeded" in low
    )


class OpenAIResponsesAdapter:
    provider = "openai"
    # OpenAI reports cached tokens as a SUBSET of input tokens (Anthropic's
    # are disjoint buckets) — cost accounting needs to know.
    cached_in_input = True

    def __init__(
        self,
        *,
        model_key: str,
        model_id: str,
        system_prompt: str,
        effort_label: str | None,
        session_id: str = "",
        body: dict | None = None,
    ):
        from server.openai_bedrock import runtime as oai

        self.oai = oai
        self.model_key = model_key
        self.model_id = model_id
        self.session_id = session_id
        self.instructions = system_prompt + oai.GPT_INSTRUCTIONS_SUFFIX
        self.effort = oai.reasoning_effort_for(model_key, effort_label)
        self.verbosity = oai.verbosity_for(model_key, body)
        self.region = oai.region_for(model_key)

    # ── Canonical → Responses input conversion ───────────────────────────────

    def to_input_items(self, messages: list[dict]) -> list[dict]:
        oai = self.oai
        items: list[dict] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "assistant" and isinstance(content, list):
                text_parts = [
                    b
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                ]
                if text_parts:
                    items.append(
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": b["text"]} for b in text_parts
                            ],
                        }
                    )
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        items.append(
                            {
                                "type": "function_call",
                                "call_id": b.get("id", ""),
                                "name": b.get("name", ""),
                                "arguments": json.dumps(b.get("input") or {}),
                            }
                        )
                # thinking blocks are not replayable on this endpoint: skipped.
                continue
            if (
                role == "user"
                and isinstance(content, list)
                and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            ):
                passthrough: list[dict] = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        items.extend(
                            oai.tool_result_input_items(
                                b.get("tool_use_id", ""), b.get("content", "")
                            )
                        )
                    else:
                        passthrough.append(b)
                if passthrough:
                    items.extend(oai.to_responses_input([{"role": role, "content": passthrough}]))
                continue
            items.extend(oai.to_responses_input([m]))
        return items

    def _translate_tools(self, tools: list[dict]) -> list[dict]:
        pool = [t for t in tools if t.get("name") not in self.oai.EXCLUDED_TOOLS]
        return self.oai.translate_tools(pool)

    # ── One streamed round ────────────────────────────────────────────────────

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        oai = self.oai
        input_items = self.to_input_items(messages)
        oa_tools = self._translate_tools(tools) if tools else None
        # Stable per-session cache key: the endpoint reuses the cached
        # system+tools prefix across the session's turns.
        session_key = f"ws-{self.session_id or self.model_key}"

        client = oai.build_client(self.region)
        try:
            stream = await client.responses.create(
                model=self.model_id,
                instructions=self.instructions,
                input=input_items,
                reasoning={"effort": self.effort, "summary": "auto"},
                text={"verbosity": self.verbosity},
                stream=True,
                store=False,
                prompt_cache_key=session_key,
                **(
                    {"tools": oa_tools, "tool_choice": "none" if is_last_round else "auto"}
                    if oa_tools
                    else {}
                ),
            )
        except Exception as e:  # noqa: BLE001 — classify, then surface or rescue
            msg = str(e)
            if _is_prompt_too_long(msg):
                raise PromptTooLongError(msg) from e
            log.warning("OpenAI responses.create failed (%s): %s", self.model_key, e)
            yield RoundError(message=_friendly_error(e))
            return

        loop = asyncio.get_event_loop()
        q: asyncio.Queue = asyncio.Queue()

        async def produce():
            try:
                async for ev in stream:
                    q.put_nowait(("ev", ev))
            except Exception as e:  # noqa: BLE001 — surfaced to the consumer
                q.put_nowait(("exc", e))
            finally:
                q.put_nowait(("end", None))

        prod = asyncio.ensure_future(produce())

        fcalls: dict[str, dict] = {}
        n_items = 0
        n_done = 0
        text_done = False
        out_chars = 0
        in_tok = out_tok = cached_tok = 0
        completed = False
        thinking_open = False
        text_blocks: list[str] = []
        cur_text = ""
        refusal_text = ""
        last_event = loop.time()
        last_hb = loop.time()

        try:
            while True:
                ready = text_done or (n_items > 0 and n_done >= n_items)
                try:
                    kind, payload = await asyncio.wait_for(q.get(), timeout=_POLL_S)
                except asyncio.TimeoutError:
                    now = loop.time()
                    # Answer complete -> skip mantle's laggy completed-tail.
                    if ready and (now - last_event) > _EARLY_RELEASE_GRACE_S:
                        break
                    if (now - last_hb) >= _HEARTBEAT_S:
                        last_hb = now
                        yield Heartbeat()
                    continue
                last_hb = loop.time()
                if kind == "end":
                    break
                if kind == "exc":
                    msg = str(payload)
                    if _is_prompt_too_long(msg):
                        raise PromptTooLongError(msg) from payload
                    yield RoundError(message=_friendly_error(payload))
                    return

                ev = payload
                et = getattr(ev, "type", "") or ""
                if et == "response.output_text.delta":
                    d = getattr(ev, "delta", "") or ""
                    if thinking_open:
                        thinking_open = False
                        yield ThinkingStop()
                    out_chars += len(d)
                    cur_text += d
                    last_event = loop.time()
                    yield TextDelta(text=d)
                elif et == "response.output_text.done":
                    text_done = True
                    if cur_text:
                        text_blocks.append(cur_text)
                        cur_text = ""
                    last_event = loop.time()
                elif et in (
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                ):
                    d = getattr(ev, "delta", "") or ""
                    if d:
                        if not thinking_open:
                            thinking_open = True
                            yield ThinkingStart()
                        last_event = loop.time()
                        yield ThinkingDelta(text=d)
                elif et == "response.refusal.delta":
                    d = getattr(ev, "delta", "") or ""
                    out_chars += len(d)
                    refusal_text += d
                    last_event = loop.time()
                    yield TextDelta(text=d)
                elif et == "response.output_item.added":
                    item = getattr(ev, "item", None)
                    if item is not None and getattr(item, "type", "") == "function_call":
                        n_items += 1
                        fcalls[getattr(item, "id", "") or ""] = {
                            "call_id": getattr(item, "call_id", "") or "",
                            "name": getattr(item, "name", "") or "",
                            "args": "",
                        }
                        last_event = loop.time()
                        yield ToolCallStart(name=getattr(item, "name", "") or "")
                elif et == "response.function_call_arguments.delta":
                    fc = fcalls.get(getattr(ev, "item_id", "") or "")
                    if fc is not None:
                        fc["args"] += getattr(ev, "delta", "") or ""
                        last_event = loop.time()
                elif et == "response.function_call_arguments.done":
                    fc = fcalls.get(getattr(ev, "item_id", "") or "")
                    if fc is not None:
                        da = getattr(ev, "arguments", None)
                        if da:
                            fc["args"] = da
                        try:
                            parsed = json.loads(fc["args"]) if fc["args"].strip() else {}
                        except Exception:
                            parsed = {}
                        fc["parsed"] = parsed
                        yield ToolCall(id=fc["call_id"], name=fc["name"], input=parsed)
                    n_done += 1
                    last_event = loop.time()
                elif et == "response.completed":
                    completed = True
                    resp = getattr(ev, "response", None)
                    usage = getattr(resp, "usage", None)
                    in_tok = int(getattr(usage, "input_tokens", 0) or 0)
                    out_tok = int(getattr(usage, "output_tokens", 0) or 0)
                    itd = getattr(usage, "input_tokens_details", None)
                    cached_tok = int(getattr(itd, "cached_tokens", 0) or 0)
                    break
                elif et in ("response.failed", "error"):
                    resp = getattr(ev, "response", None)
                    err = (
                        getattr(resp, "error", None) or getattr(ev, "message", "") or "stream error"
                    )
                    msg = str(getattr(err, "message", err))
                    if _is_prompt_too_long(msg):
                        raise PromptTooLongError(msg)
                    yield RoundError(message=msg)
                    return
                elif et == "response.incomplete":
                    yield Incomplete()

            if thinking_open:
                yield ThinkingStop()
        finally:
            prod.cancel()
            try:
                await stream.close()
            except Exception:  # noqa: BLE001 — abandoning the tail
                pass

        if cur_text:
            text_blocks.append(cur_text)
        if refusal_text:
            text_blocks.append(refusal_text)

        content: list[dict] = [{"type": "text", "text": t} for t in text_blocks if t]
        valid = [f for f in fcalls.values() if f.get("name")]
        for f in valid:
            content.append(
                {
                    "type": "tool_use",
                    "id": f["call_id"],
                    "name": f["name"],
                    "input": f.get("parsed", {}),
                }
            )

        yield RoundResult(
            stop_reason="tool_use" if valid else "end_turn",
            content=content,
            usage=Usage(
                input_tokens=in_tok if completed else 0,
                output_tokens=out_tok if completed else max(1, out_chars // 4),
                cache_read_tokens=cached_tok,
                exact=completed,
            ),
        )


def _friendly_error(e: Exception) -> str:
    """Actionable text for the common GPT-on-Bedrock failure classes."""
    msg = str(e)
    low = msg.lower()
    if any(s in low for s in ("auth", "401", "403", "denied", "credential", "token")):
        return (
            "GPT-5.x auth/access failed. Enable OpenAI model access in the "
            "Bedrock console for this region, confirm your AWS credentials are "
            f"valid, and that the model runs in the configured region. ({msg[:200]})"
        )
    if any(s in low for s in ("not found", "404", "does not exist", "no such model")):
        return (
            "GPT-5.x not found in this region. GPT-5.5 is served only in "
            "us-east-1 / us-east-2 (GPT-5.4 adds us-west-2). Point bedrock_region "
            "at a supported region, or set the model's openai_region override to "
            f"one. ({msg[:200]})"
        )
    return f"OpenAI (Bedrock) request failed: {msg[:240]}"
