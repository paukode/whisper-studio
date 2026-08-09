"""On-device (llama-server) adapter: OpenAI chat-completions SSE in, neutral
round events out.

Owns everything local-specific:

  - canonical messages → OpenAI chat messages with native tool fidelity
    (assistant ``tool_use`` blocks become ``tool_calls``, tool_result user
    messages become ``role: tool`` messages — the chat template then renders
    them as real tool traffic instead of user text). Images are dropped
    (vision is not wired locally); thinking blocks are not replayable.
  - loose-argument coercion against the declared schema before a call reaches
    an executor (small models emit "5" for 5 etc.).
  - context-overflow detection: llama-server rejections raise
    ``PromptTooLongError`` so the engine's reactive rescue + salvage round
    apply to local turns too (compaction summarizes via the resident local
    model through one_shot — the path stays fully offline).
  - the model-quality quirk where a fine-tune ends its turn with neither text
    nor a tool call: surfaced as a legible message instead of an empty bubble.

The llama-server SSE parsing (``_stream_round``, ``_CallAccumulator``) and the
message/coercion helpers live in server/local/server_stream.py and are reused
here — this adapter only bridges them to the engine's event vocabulary.
"""

import json
import logging

from server.infrastructure.errors import PromptTooLongError

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

_CTX_OVERFLOW_MARKERS = ("exceed_context_size", "context size", "too long", "context window")


class LocalAdapter:
    provider = "local"

    def __init__(
        self,
        *,
        model_key: str,
        base_url: str,
        system_prompt: str,
        thinking: bool,
        tools_enabled: bool,
    ):
        self.model_key = model_key
        self.base_url = base_url
        self.system_prompt = system_prompt
        self.thinking = thinking
        self.tools_enabled = tools_enabled
        # Per-model output cap: chat_model_meta.max_output when configured,
        # else a conservative local default.
        from server.infrastructure.config import load_config

        meta = (load_config().get("chat_model_meta") or {}).get(model_key) or {}
        try:
            self.max_tokens = int(meta.get("max_output") or 4096)
        except (TypeError, ValueError):
            self.max_tokens = 4096
        # Cumulative visible-text chars across the TURN (all rounds), for the
        # empty-turn quirk message.
        self._turn_out_chars = 0

    # ── Canonical → OpenAI chat conversion ───────────────────────────────────

    def to_openai_messages(self, messages: list[dict]) -> list[dict]:
        from server.local.server_stream import _tool_results_to_messages

        out: list[dict] = []
        if self.system_prompt:
            out.append({"role": "system", "content": self.system_prompt})
        names_by_id: dict[str, str] = {}
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "assistant" and isinstance(content, list):
                text = "".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                calls = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
                if calls:
                    for c in calls:
                        names_by_id[c.get("id", "")] = c.get("name", "tool")
                    out.append(
                        {
                            "role": "assistant",
                            "content": text or None,
                            "tool_calls": [
                                {
                                    "id": c.get("id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": c.get("name", ""),
                                        "arguments": json.dumps(c.get("input") or {}),
                                    },
                                }
                                for c in calls
                            ],
                        }
                    )
                elif text.strip():
                    out.append({"role": "assistant", "content": text})
                continue
            if (
                role == "user"
                and isinstance(content, list)
                and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            ):
                results = [
                    {"tool_use_id": b.get("tool_use_id", ""), "content": b.get("content", "")}
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                out.extend(_tool_results_to_messages(results, names_by_id))
                extra = "".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                if extra.strip():
                    out.append({"role": "user", "content": extra})
                continue
            # Plain turn: string content, or text blocks (images dropped —
            # vision is not wired here).
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                text = ""
            if text.strip():
                out.append({"role": role, "content": text})
        return out

    # ── One streamed round ────────────────────────────────────────────────────

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        from server.local.server_stream import (
            _coerce_call_args,
            _schema_props,
            _stream_round,
        )
        from server.local.tools import _to_openai_fn

        schemas = [_to_openai_fn(t) for t in tools] if (tools and self.tools_enabled) else []
        props_by_tool = _schema_props(schemas)

        # Some fine-tunes decode a handful of tokens and stop without ever
        # entering plain-text or emitting a tool call — seen most often right
        # after a tool result. One retry with an explicit "continue" nudge
        # recovers most of these for free: llama-server's prefix cache means
        # the retry re-processes only the tiny nudge message, not the whole
        # prompt (confirmed: a stalled round's retry re-prompts in the tens,
        # not thousands, of tokens).
        retry_messages = messages
        for attempt in range(2):
            payload: dict = {
                "model": self.model_key,
                "messages": self.to_openai_messages(retry_messages),
                "max_tokens": self.max_tokens,
            }
            if schemas:
                # Final-round parity with the cloud paths: forbid calls on the
                # last round so the model synthesizes from what it has.
                payload["tools"] = schemas
                payload["tool_choice"] = "none" if is_last_round else "auto"
            if self.thinking:
                # Some builds gate reasoning behind an explicit request; harmless
                # when the model has no thinking mode.
                payload["chat_template_kwargs"] = {"enable_thinking": True}

            thinking_open = False
            calls: list[dict] = []
            round_usage: dict | None = None
            round_chars = 0
            round_text = ""
            try:
                async for kind, piece in _stream_round(self.base_url, payload):
                    if kind == "thinking":
                        if not thinking_open:
                            thinking_open = True
                            yield ThinkingStart()
                        yield ThinkingDelta(text=piece)
                    elif kind == "text":
                        if thinking_open:
                            thinking_open = False
                            yield ThinkingStop()
                        round_chars += len(piece)
                        round_text += piece
                        self._turn_out_chars += len(piece)
                        yield TextDelta(text=piece)
                    else:  # ("done", {...})
                        calls = piece.get("calls") or []
                        round_usage = piece.get("usage")
            except Exception as e:
                if thinking_open:
                    yield ThinkingStop()
                msg = str(e)
                if any(marker in msg.lower() for marker in _CTX_OVERFLOW_MARKERS):
                    raise PromptTooLongError(msg) from e
                log.warning("llama-server round %d failed: %s", round_num, e)
                yield RoundError(message=msg)
                return
            if thinking_open:  # reasoned but produced no answer text this round
                yield ThinkingStop()

            if calls or round_text or attempt == 1:
                break
            log.info(
                "Local turn (%s) got an empty completion at round %d; "
                "retrying once with a continuation nudge.",
                self.model_key,
                round_num,
            )
            retry_messages = [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "(Continue: give your final answer now, in plain text, "
                        "based on the tool result above. Do not call another "
                        "tool unless it is truly necessary.)"
                    ),
                },
            ]

        # Normalize loose argument types against the declared schema before
        # the call reaches an executor (and before it enters history).
        content: list[dict] = []
        for c in calls:
            c["input"] = _coerce_call_args(c["name"], c.get("input") or {}, props_by_tool)
            yield ToolCallStart(name=c["name"])
            yield ToolCall(id=c["id"], name=c["name"], input=c["input"])

        if not calls and self._turn_out_chars == 0:
            # Model-quality failure made legible: the turn ended with neither
            # text nor a tool call (seen on some community fine-tunes after a
            # tool result). Not a substitute for an answer.
            log.warning(
                "Local turn (%s) ended with no text and no tool call at round %d.",
                self.model_key,
                round_num,
            )
            note = (
                "(No answer: the model ended its turn without producing any "
                "text. Try again, or switch model — some fine-tunes stall "
                "after a tool result.)"
            )
            self._turn_out_chars += len(note)
            yield TextDelta(text=note)
            content.append({"type": "text", "text": note})

        if round_text:
            content.insert(0, {"type": "text", "text": round_text})
        for c in calls:
            content.append(
                {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
            )

        # Usage semantics match the cloud frame (and the old local tally):
        # llama.cpp reports the FULL prompt in prompt_tokens and the KV-reused
        # prefix in prompt_tokens_details.cached_tokens; input_tokens is the
        # difference (what was actually prefilled), cache_read the reused
        # prefix. Summing full prompts would recount the conversation every
        # tool round. No usage (older builds) → chars/4 estimate.
        usage = round_usage if isinstance(round_usage, dict) else {}
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details")
        cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
        cached = min(cached, prompt)  # a buggy build must not go negative
        if prompt or completion:
            round_usage_ev = Usage(
                input_tokens=prompt - cached,
                output_tokens=completion,
                cache_read_tokens=cached,
                exact=True,
            )
        else:
            round_usage_ev = Usage(
                input_tokens=0, output_tokens=max(1, round_chars // 4), exact=False
            )
        yield RoundResult(
            stop_reason="tool_use" if calls else "end_turn",
            content=content,
            usage=round_usage_ev,
        )
