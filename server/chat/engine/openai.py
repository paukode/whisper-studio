"""OpenAI-on-Bedrock (Responses API) adapter: mantle wire protocol in, neutral
round events out.

Owns everything GPT/mantle-specific:

  - canonical messages → Responses ``input`` items with full function-call
    fidelity (assistant ``tool_use`` blocks become ``function_call`` items and
    tool_result user messages become ``function_call_output`` items, so the
    call linkage survives round-tripping through the engine's canonical
    format)
  - the two mantle stream adaptations, verified live: EARLY RELEASE (mantle
    can hold the stream open for seconds after the output before
    ``completed``; the round moves on once a ``final_answer`` message or every
    function call is done, never on a ``commentary`` preamble, which the calls
    follow) and HEARTBEAT keepalives during idle gaps
  - prompt-too-long detection: mantle rejections like ``prompt tokens (281096)
    exceed customer model maximum (278528)`` raise ``PromptTooLongError`` so
    the engine's bounded, fail-closed compaction applies to GPT turns exactly
    as it does to Claude
  - effort mapping and the tool exclusions (EXCLUDED_TOOLS)

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
    TOOL_ARGS_PROGRESS_STEP,
    Heartbeat,
    Incomplete,
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

_POLL_S = 0.25
_HEARTBEAT_S = 5.0
_EARLY_RELEASE_GRACE_S = 1.0


def _is_prompt_too_long(message: str, error=None) -> bool:
    """Recognize structured provider codes as well as documented limit messages."""
    code = error.get("code") if isinstance(error, dict) else getattr(error, "code", None)
    body = error if isinstance(error, dict) else getattr(error, "body", None)
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict):
            code = detail.get("code") or code
    low = (message or "").lower()
    return (
        code == "context_length_exceeded"
        or windows.parse_prompt_cap(message) is not None
        or "too long" in low
        or ("prompt tokens" in low and "exceed" in low)
        or ("context length" in low and "exceed" in low)
        or "context_length_exceeded" in low
    )


# Faults that pass the 200-OK gate and then kill the Responses stream (a
# ``response.failed`` event or a transport exception mid-read). The SDK's
# retry layer never sees these, so the engine re-runs the round when the
# message matches. "server had an error" is OpenAI's stock server_error text
# — the exact failure observed live on bedrock-mantle after 5 tool rounds.
_TRANSIENT_MARKERS = (
    "server had an error",
    "server_error",
    "internal error",
    "internal server error",
    "overloaded",
    "throttl",
    "rate limit",
    "too many requests",
    "service unavailable",
    # OpenAI's stock 503 text is "The service is temporarily unavailable." —
    # "service unavailable" is not a substring of it, so it was judged
    # permanent and a round-0 fault ended the turn with zero retries.
    "temporarily unavailable",
    "unavailable",
    "bad gateway",
    "gateway timeout",
    "timed out",
    "timeout",
    "connection",
    "429",
    "500",
    "502",
    "503",
    "504",
)


def _is_transient(message: str, exc: BaseException | None = None) -> bool:
    """Transient (retry the round) vs permanent (end the turn).

    Judged on the message text, plus the exception's class name when one is
    given: a transport fault like ``httpx.ReadTimeout('')`` stringifies to
    nothing at all, so message matching alone called it permanent and the
    user saw a round die with zero retries.
    """
    low = (message or "").lower()
    if exc is not None:
        low = f"{low} {type(exc).__name__.lower()}"
    return any(m in low for m in _TRANSIENT_MARKERS)


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
    ):
        from server.openai_bedrock import runtime as oai

        self.oai = oai
        self.model_key = model_key
        self.model_id = model_id
        self.session_id = session_id
        self.instructions = system_prompt + oai.GPT_INSTRUCTIONS_SUFFIX
        self.effort = oai.reasoning_effort_for(model_key, effort_label)
        self.region = oai.region_for(model_key)
        # The round in flight, for inflight_usage (None between rounds).
        self._inflight = None

    def describe_request(self) -> dict:
        """Provider half of a request snapshot (server/chat/request_snapshots.py)."""
        return {
            "instructions": self.instructions,
            "reasoning_effort": self.effort,
            "region": self.region,
        }

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

    def inflight_usage(self) -> Usage | None:
        """What the round in flight has cost so far, for the engine to record
        when the round ends without a RoundResult (a mid-stream failure, a
        retry, a Stop): the usage a failure event carried, else characters / 4
        of the posted request and the content received. Once the stream has
        delivered any event the provider is processing the prompt, so the
        input is billed even while only reasoning (whose summary is not
        billed output) has arrived. None before the first event: no evidence
        the prompt was ever processed."""
        from server.costs.capture import estimated_counts, responses_counts

        inflight = getattr(self, "_inflight", None)
        if inflight is None:
            return None
        received = inflight["received"] + sum(len(a) for a in inflight["args"].values())
        if inflight["usage"] is not None:
            return Usage(**responses_counts(inflight["usage"], inflight["request_chars"], received))
        if not inflight["acknowledged"]:
            return None
        return Usage(**estimated_counts(inflight["request_chars"], received))

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        from server.costs.capture import json_chars, responses_counts

        oai = self.oai
        input_items = self.to_input_items(messages)
        oa_tools = self._translate_tools(tools) if tools else None
        # Stable per-session cache key: the endpoint reuses the cached
        # system+tools prefix across the session's turns.
        session_key = f"ws-{self.session_id or self.model_key}"
        request = {
            "model": self.model_id,
            "instructions": self.instructions,
            "input": input_items,
            "reasoning": {"effort": self.effort, "summary": "auto"},
            "stream": True,
            "store": False,
            "prompt_cache_key": session_key,
            **(
                {"tools": oa_tools, "tool_choice": "none" if is_last_round else "auto"}
                if oa_tools
                else {}
            ),
        }
        # The request body's characters are the input estimate when the
        # usage-bearing completed event never arrives (the early release).
        # ``acknowledged``: the stream delivered an event, so the prompt is
        # being processed (and billed) whatever arrives after it.
        inflight = {
            "request_chars": json_chars(request),
            "received": 0,
            "args": {},
            "usage": None,
            "acknowledged": False,
        }
        self._inflight = inflight

        client = oai.build_client(self.region)
        try:
            stream = await client.responses.create(**request)
        except Exception as e:  # noqa: BLE001 — classify, then surface or rescue
            msg = str(e)
            if _is_prompt_too_long(msg, e):
                raise PromptTooLongError(msg) from e
            log.warning("OpenAI responses.create failed (%s): %s", self.model_key, e)
            # The SDK already retried the request once. A connection that
            # still fails (a field turn ended on "Connection error." right
            # after a long sub-agent wait) gets the engine's round retries,
            # with their backoff, the way a fault mid-stream does.
            yield RoundError(
                message=_friendly_error(e, self.model_key, self.region),
                retryable=_is_transient(msg, e),
            )
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

        from .response_calls import ResponseCalls

        calls = ResponseCalls()
        # Only final_answer text can release early, never a commentary preamble
        # or text that accompanies a known unfinished function call.
        msg_phase = None
        answer_done = False
        response_finished = False
        incomplete_reason = None
        thinking_open = False
        accepted = False
        text_blocks: list[str] = []
        cur_text = ""
        refusal_text = ""
        last_event = loop.time()
        last_hb = loop.time()

        try:
            while True:
                ready = calls.complete or (answer_done and not calls.seen)
                try:
                    kind, payload = await asyncio.wait_for(q.get(), timeout=_POLL_S)
                except TimeoutError:
                    now = loop.time()
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
                    if _is_prompt_too_long(msg, payload):
                        raise PromptTooLongError(msg) from payload
                    log.warning("OpenAI stream broke mid-round (%s): %r", self.model_key, payload)
                    yield RoundError(
                        message=calls.error
                        or _friendly_error(payload, self.model_key, self.region),
                        retryable=not calls.error and _is_transient(msg, payload),
                    )
                    return

                ev = payload
                inflight["acknowledged"] = True
                et = getattr(ev, "type", "") or ""
                if et in ("response.created", "response.in_progress"):
                    # Mantle can reason silently before it sends any summary.
                    if not accepted and not thinking_open and self.effort != "none":
                        thinking_open = True
                        yield ThinkingStart()
                    accepted = True
                elif et == "response.output_text.delta":
                    d = getattr(ev, "delta", "") or ""
                    if thinking_open:
                        thinking_open = False
                        yield ThinkingStop()
                    inflight["received"] += len(d)
                    cur_text += d
                    last_event = loop.time()
                    yield TextDelta(text=d)
                elif et == "response.output_text.done":
                    answer_done = answer_done or msg_phase == "final_answer"
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
                        if et == "response.reasoning_text.delta":
                            # Raw reasoning is billed output; a summary is not.
                            inflight["received"] += len(d)
                        if not thinking_open:
                            thinking_open = True
                            yield ThinkingStart()
                        last_event = loop.time()
                        yield ThinkingDelta(text=d)
                elif et == "response.refusal.delta":
                    d = getattr(ev, "delta", "") or ""
                    inflight["received"] += len(d)
                    refusal_text += d
                    last_event = loop.time()
                    yield TextDelta(text=d)
                elif et == "response.output_item.added":
                    item = getattr(ev, "item", None)
                    item_type = getattr(item, "type", "")
                    if item_type == "message":
                        msg_phase = getattr(item, "phase", None)
                    elif item_type == "function_call":
                        fc = calls.start(item)
                        last_event = loop.time()
                        if fc is not None:
                            yield ToolCallStart(name=fc["name"], id=fc["call_id"])
                elif et == "response.function_call_arguments.delta":
                    fc = calls.delta(getattr(ev, "item_id", None), getattr(ev, "delta", None))
                    last_event = loop.time()
                    if fc is not None:
                        inflight["args"][id(fc)] = fc["args"]
                        # Large tool arguments retain streamed size progress.
                        _n = len(fc["args"])
                        if _n >= fc.get("next_progress", TOOL_ARGS_PROGRESS_STEP):
                            fc["next_progress"] = (
                                _n // TOOL_ARGS_PROGRESS_STEP + 1
                            ) * TOOL_ARGS_PROGRESS_STEP
                            yield ToolCallProgress(name=fc["name"], chars=_n, id=fc["call_id"])
                elif et == "response.function_call_arguments.done":
                    fc = calls.finish(getattr(ev, "item_id", None), getattr(ev, "arguments", None))
                    inflight["args"] = {id(c): c["args"] for c in calls.calls.values()}
                    if fc is not None:
                        yield ToolCall(id=fc["call_id"], name=fc["name"], input=fc["parsed"])
                    last_event = loop.time()
                elif et == "response.output_item.done":
                    calls.check_item(getattr(ev, "item", None))
                    last_event = loop.time()
                elif et in ("response.completed", "response.incomplete"):
                    response_finished = True
                    resp = getattr(ev, "response", None)
                    # Retain reported counts even if arguments force a rejection.
                    inflight["usage"] = getattr(resp, "usage", None)
                    terminal_output = getattr(resp, "output", None)
                    if terminal_output is not None:
                        calls.check_snapshot(terminal_output)
                    if et == "response.incomplete":
                        incomplete_reason = (
                            getattr(getattr(resp, "incomplete_details", None), "reason", None)
                            or "unknown reason"
                        )
                    break
                elif et in ("response.failed", "error"):
                    resp = getattr(ev, "response", None)
                    inflight["usage"] = getattr(resp, "usage", None)
                    err = (
                        getattr(resp, "error", None) or getattr(ev, "message", "") or "stream error"
                    )
                    msg = str(getattr(err, "message", err))
                    if _is_prompt_too_long(msg, err):
                        raise PromptTooLongError(msg)
                    log.warning("OpenAI stream failed (%s, event=%s): %s", self.model_key, et, msg)
                    yield RoundError(
                        message=calls.error or msg, retryable=not calls.error and _is_transient(msg)
                    )
                    return

            if thinking_open:
                yield ThinkingStop()
        finally:
            prod.cancel()
            try:
                await stream.close()
            except Exception:  # noqa: BLE001 - abandoning the tail
                pass

        failure = calls.failure(response_finished=response_finished)
        if incomplete_reason:
            reason = (
                "output token limit reached"
                if incomplete_reason == "max_output_tokens"
                else str(incomplete_reason)
            )
            if calls.seen or incomplete_reason != "max_output_tokens":
                failure = f"Response incomplete ({reason})."
            else:
                yield Incomplete()
        if failure:
            yield RoundError(message=f"{failure} No tools from this round were run.")
            return

        if cur_text:
            text_blocks.append(cur_text)
        if refusal_text:
            text_blocks.append(refusal_text)
        content: list[dict] = [{"type": "text", "text": t} for t in text_blocks if t]
        for fc in calls.calls.values():
            content.append(
                {"type": "tool_use", "id": fc["call_id"], "name": fc["name"], "input": fc["parsed"]}
            )

        received = inflight["received"] + sum(len(a) for a in inflight["args"].values())
        usage_counts = responses_counts(inflight["usage"], inflight["request_chars"], received)
        # Only successful rounds transfer their usage to RoundResult. A rejected
        # round stays in flight for the runner's once-only error accounting.
        self._inflight = None
        yield RoundResult(
            stop_reason="tool_use" if calls.calls else "end_turn",
            content=content,
            usage=Usage(**usage_counts),
        )


def _friendly_error(e: Exception, model_key: str = "", region: str = "") -> str:
    """Actionable errors, without confusing output/context tokens with credentials."""
    import re

    msg = str(e) or f"{type(e).__name__} (the model stopped responding mid-stream)"
    low = msg.lower()
    status = getattr(e, "status_code", None)
    if status is None:
        # SDK errors without a structured status still identify HTTP codes
        # explicitly. Arbitrary numbers in a token count are not status codes.
        found = re.search(r"(?:error code|status(?: code)?|http)\s*[:=]?\s*(\d{3})\b", low)
        status = int(found[1]) if found else None
    kind = type(e).__name__.lower()
    credential_markers = (
        "accessdenied",
        "access denied",
        "not authorized",
        "unauthorized",
        "authentication failed",
        "invalid api key",
        "invalid_api_key",
        "invalid bearer token",
        "expired token",
        "expiredtoken",
        "invalid security token",
        "security token included in the request is invalid",
        "unable to locate credentials",
        "invalid credentials",
        "expired credentials",
    )
    if (
        status in (401, 403)
        or kind
        in (
            "authenticationerror",
            "permissiondeniederror",
            "nocredentialserror",
            "partialcredentialserror",
        )
        or any(s in low for s in credential_markers)
    ):
        return (
            "GPT auth/access failed. Enable OpenAI model access in the "
            "Bedrock console for this region, confirm your AWS credentials are "
            f"valid, and that the model runs in the configured region. ({msg[:200]})"
        )
    if status == 404 or any(s in low for s in ("not found", "does not exist", "no such model")):
        from server.openai_bedrock.runtime import not_found_advice

        return f"{not_found_advice(model_key, region)} ({msg[:200]})"
    return f"OpenAI (Bedrock) request failed: {msg[:240]}"
