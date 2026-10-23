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
  - rounds the output cap cut off (finish_reason ``length``): continued when
    cut mid-answer, otherwise ended with a note naming the limit. A tool call
    cut off mid-arguments is never run.

The llama-server SSE parsing (``_stream_round``, ``_CallAccumulator``) and the
message/coercion helpers live in server/local/server_stream.py and are reused
here — this adapter only bridges them to the engine's event vocabulary.
"""

import json
import logging
import re

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


def _joined_text(content: list) -> str:
    """The text blocks of a canonical content list, a blank line between them.

    Blocks are separate on purpose (the user's words, then a reminder or a
    mid-turn note the engine appended), so they are never glued together.
    A block whose text is missing or null counts as empty.
    """
    return "\n\n".join(
        b.get("text") or ""
        for b in content
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
    )


def _is_plain(m: dict) -> bool:
    return (
        m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
        and not m.get("tool_calls")
    )


def _alternating(out: list[dict]) -> list[dict]:
    """Shape the wire messages the way strict chat templates require.

    Several sources put two plain messages of one role side by side: an empty
    assistant row dropped from the history, the attachment message that leads
    the first user turn, the text after tool results, the empty-completion
    nudge. Strict templates (Gemma 2 and 3 style) reject that, so such
    neighbours are merged with a blank line between them. Tool traffic
    (assistant tool_calls, tool messages) is never merged. A history window
    that opens on an assistant reply starts at the first user turn instead,
    as compaction's ensure_valid_start does.
    """
    merged: list[dict] = []
    for m in out:
        prev = merged[-1] if merged else None
        if prev is not None and _is_plain(prev) and _is_plain(m) and prev["role"] == m["role"]:
            merged[-1] = {**prev, "content": f"{prev['content']}\n\n{m['content']}"}
            continue
        merged.append(m)
    first = 1 if merged and merged[0].get("role") == "system" else 0
    while (
        first < len(merged)
        and _is_plain(merged[first])
        and merged[first]["role"] == "assistant"
        and any(m.get("role") == "user" for m in merged[first + 1 :])
    ):
        del merged[first]
    return merged


def _continue_nudge(messages: list[dict]) -> dict:
    """The one-shot retry message for an empty completion, worded for what
    the model is answering. Request-only by design: a recovery retry within
    one round, never persisted, so invariant 3 (persisted reminders) does not
    apply. It merges into a trailing user message on the wire."""
    tail = messages[-1] if messages else {}
    content = tail.get("content")
    after_tool = (
        tail.get("role") == "user"
        and isinstance(content, list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )
    if after_tool:
        text = (
            "(Continue: give your final answer now, in plain text, based on the "
            "tool result above. Do not call another tool unless it is truly necessary.)"
        )
    else:
        text = "(Continue: reply to the message above now, in plain text.)"
    return {"role": "user", "content": text}


# A short reply that says what the model will do next and then stops. The
# loop resumes a model only after a tool call, so such a reply ends the turn
# with nothing done. Measured 2026-10-23: Ministral 3 14B answered "can you
# review this code for me?" with "Let me first list the files ... I'll then
# read and analyze them" and no call in 2 of 5 runs; a Gemma 4 coder
# fine-tune did it on most turns. Asking the user something is a real reason
# to stop, so a question or a request for input never counts.
_ANNOUNCES = re.compile(
    r"\b(?:I'll|I will|I'm going to|I am going to|let me(?! know)|let's|first,? I|next,? I"
    r"|to get started)\b",
    re.IGNORECASE,
)
_ASKS_USER = re.compile(
    r"\?|\b(?:please (?:provide|share|tell|paste|specify)|could you|can you|would you"
    r"|let me know)\b",
    re.IGNORECASE,
)
_ANNOUNCE_MAX_CHARS = 600

_ANNOUNCE_NUDGE = (
    "(Continue: you said what you will do next, but your reply has no tool call. "
    "Make that tool call now. If no tool is needed, give your final answer instead.)"
)


def _announces_next_step(text: str) -> bool:
    """Whether ``text`` only announces a next step (see _ANNOUNCES)."""
    t = text.strip()
    return (
        0 < len(t) <= _ANNOUNCE_MAX_CHARS
        and _ANNOUNCES.search(t) is not None
        and _ASKS_USER.search(t) is None
    )


def _summed_usage(usages: list) -> dict | None:
    """One round's llama-server usage across its attempts: the retry's prompt
    reuses the cached prefix, so summing counts each prefilled token once."""
    rows = [u for u in usages if isinstance(u, dict)]
    if not rows:
        return None

    def cached(u):
        d = u.get("prompt_tokens_details")
        return int(d.get("cached_tokens") or 0) if isinstance(d, dict) else 0

    return {
        "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in rows),
        "completion_tokens": sum(int(u.get("completion_tokens") or 0) for u in rows),
        "prompt_tokens_details": {"cached_tokens": sum(cached(u) for u in rows)},
    }


class LocalAdapter:
    provider = "local"
    # Set by the engine each round: called with ``(body, attempt)`` for every
    # request this adapter posts after the first one of the round (the
    # empty-completion retry), so request snapshots cover it (runner.py).
    on_retry_request = None

    def __init__(
        self,
        *,
        model_key: str,
        base_url: str,
        system_prompt: str,
        thinking: bool,
        tools_enabled: bool,
        wire_model: str | None = None,
        supports_tool_choice: bool = True,
    ):
        self.model_key = model_key
        self.base_url = base_url
        self.system_prompt = system_prompt
        self.thinking = thinking
        self.tools_enabled = tools_enabled
        # What the `model` field on the wire says. llama-server just echoes it;
        # mlx_lm maps "default_model" to its --model path and treats any OTHER
        # name as a new repo to load, so the registry key must not leak there.
        self.wire_model = wire_model or model_key
        # mlx_lm ignores OpenAI `tool_choice`, so the final-round "no more
        # tools" contract is enforced by omitting the tools list instead.
        self.supports_tool_choice = supports_tool_choice
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

    def describe_request(self) -> dict:
        """Provider half of a request snapshot (server/chat/request_snapshots.py)."""
        return {
            "system_prompt": self.system_prompt,
            "thinking": self.thinking,
            "tools_enabled": self.tools_enabled,
            "wire_model": self.wire_model,
            "base_url": self.base_url,
            "max_tokens": self.max_tokens,
            "supports_tool_choice": self.supports_tool_choice,
        }

    def wire_request(
        self, messages, tools, is_last_round, *, retry: bool = False, announced: str = ""
    ) -> dict:
        """The exact chat-completions body posted for a round (the transport
        adds only the stream options). ``retry`` adds the empty-completion
        nudge. ``announced`` is the reply of an attempt that only announced a
        step: it goes back as the model's own words, followed by a nudge to
        make the call. Both nudges are request-only, never persisted. Pure, so
        a request snapshot can rebuild it off the loop and record what the
        model server actually receives."""
        from server.local.tools import _to_openai_fn

        schemas = [_to_openai_fn(t) for t in tools] if (tools and self.tools_enabled) else []
        if is_last_round and not self.supports_tool_choice:
            # tool_choice:"none" would be ignored (mlx_lm): undeclare the tools
            # so the final round genuinely cannot call one. Costs this round's
            # prompt-cache prefix, but the final round is the last one anyway.
            schemas = []
        if announced:
            sent = [
                *messages,
                {"role": "assistant", "content": announced},
                {"role": "user", "content": _ANNOUNCE_NUDGE},
            ]
        elif retry:
            sent = [*messages, _continue_nudge(messages)]
        else:
            sent = messages
        body: dict = {
            "model": self.wire_model,
            "messages": self.to_openai_messages(sent),
            "max_tokens": self.max_tokens,
        }
        if schemas:
            # Final-round parity with the cloud paths: forbid calls on the
            # last round so the model synthesizes from what it has.
            body["tools"] = schemas
            body["tool_choice"] = "none" if is_last_round else "auto"
        if self.thinking:
            # Some builds gate reasoning behind an explicit request; harmless
            # when the model has no thinking mode.
            body["chat_template_kwargs"] = {"enable_thinking": True}
        return body

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
                text = _joined_text(content)
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
                extra = _joined_text(content)
                if extra.strip():
                    out.append({"role": "user", "content": extra})
                continue
            # Plain turn: string content, or text blocks (images dropped —
            # vision is not wired here).
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = _joined_text(content)
            else:
                text = ""
            if text.strip():
                out.append({"role": role, "content": text})
        return _alternating(out)

    # ── One streamed round ────────────────────────────────────────────────────

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        from server.local.server_stream import (
            _coerce_call_args,
            _schema_props,
            _stream_round,
        )

        # A round gets at most one retry, of one of two kinds. llama-server's
        # prefix cache makes either cheap: the retry re-processes only its
        # short tail, not the whole prompt (confirmed: a stalled round's retry
        # re-prompts in the tens, not thousands, of tokens).
        #   * Empty: some fine-tunes decode a handful of tokens and stop with
        #     neither text nor a tool call, most often right after a tool
        #     result. The retry adds a "continue" nudge.
        #   * Announced: a short reply that only says what the model will do
        #     next (_announces_next_step) while tools were offered. The retry
        #     sends that reply back as the model's own words with a nudge to
        #     make the call. Its output is held until it proves to be a call:
        #     the user already saw the announcement, so prose from the retry
        #     is dropped and the announcement stands as the round's answer.
        announced = ""
        spent: list = []
        round_chars = 0
        reasoning_chars = 0
        round_text = ""
        calls: list[dict] = []
        finish_reason: str | None = None
        for attempt in range(2):
            payload = self.wire_request(
                messages,
                tools,
                is_last_round,
                retry=attempt > 0 and not announced,
                announced=announced,
            )
            props_by_tool = _schema_props(payload.get("tools") or [])
            if attempt and self.on_retry_request is not None:
                try:
                    self.on_retry_request(payload, attempt)
                except Exception as e:  # noqa: BLE001 - a snapshot never costs the round
                    log.debug("retry request hook failed: %s", e)

            hold = bool(announced)
            held: list = []
            thinking_open = False
            attempt_calls: list[dict] = []
            attempt_usage: dict | None = None
            attempt_finish: str | None = None
            attempt_text = ""
            try:
                async for kind, piece in _stream_round(self.base_url, payload):
                    events: list = []
                    if kind == "thinking":
                        if not thinking_open:
                            thinking_open = True
                            events.append(ThinkingStart())
                        reasoning_chars += len(piece)
                        events.append(ThinkingDelta(text=piece))
                    elif kind == "text":
                        if thinking_open:
                            thinking_open = False
                            events.append(ThinkingStop())
                        round_chars += len(piece)
                        attempt_text += piece
                        events.append(TextDelta(text=piece))
                    else:  # ("done", {...})
                        attempt_calls = piece.get("calls") or []
                        attempt_usage = piece.get("usage")
                        attempt_finish = piece.get("finish_reason")
                    if hold:
                        held.extend(events)
                        continue
                    for ev in events:
                        if isinstance(ev, TextDelta):
                            self._turn_out_chars += len(ev.text)
                        yield ev
            except Exception as e:
                if hold:
                    # The announcement already reached the user: a failed
                    # retry leaves it as the round's answer.
                    log.warning("Local model round %d call retry failed: %r", round_num, e)
                    break
                if thinking_open:
                    yield ThinkingStop()
                msg = str(e)
                if any(marker in msg.lower() for marker in _CTX_OVERFLOW_MARKERS):
                    raise PromptTooLongError(msg) from e
                # Connection-drop exceptions often stringify to '' — surface
                # the type so neither the log nor the user gets a blank error.
                if not msg.strip():
                    msg = f"connection to the local model server was lost ({e.__class__.__name__})"
                log.warning("Local model round %d failed: %r", round_num, e)
                yield RoundError(message=msg)
                return
            if thinking_open:  # reasoned but produced no answer text this round
                if hold:
                    held.append(ThinkingStop())
                else:
                    yield ThinkingStop()
            spent.append(attempt_usage)

            if hold:
                if attempt_calls:
                    if attempt_text.strip():
                        held.insert(0, TextDelta(text="\n\n"))
                        round_text = f"{announced}\n\n{attempt_text}"
                    for ev in held:
                        if isinstance(ev, TextDelta):
                            self._turn_out_chars += len(ev.text)
                        yield ev
                    calls, finish_reason = attempt_calls, attempt_finish
                log.info(
                    "Local turn (%s) only announced a step at round %d; the retry %s.",
                    self.model_key,
                    round_num,
                    "made the call" if attempt_calls else "made no call either",
                )
                break
            calls, finish_reason, round_text = attempt_calls, attempt_finish, attempt_text
            # A round the output cap cut off is not a stall: nudging it would
            # only spend the same budget again.
            if calls or finish_reason == "length" or attempt == 1:
                break
            if round_text:
                offered = bool(payload.get("tools")) and payload.get("tool_choice") != "none"
                if not (offered and _announces_next_step(round_text)):
                    break
                announced = round_text
                log.info(
                    "Local turn (%s) only announced a step at round %d; "
                    "retrying once with a call nudge.",
                    self.model_key,
                    round_num,
                )
                continue
            log.info(
                "Local turn (%s) got an empty completion at round %d; "
                "retrying once with a continuation nudge.",
                self.model_key,
                round_num,
            )

        stop_reason = "tool_use" if calls else "end_turn"
        content: list[dict] = []
        if finish_reason == "length":
            stop_reason, calls, note = self._output_limit_outcome(round_num, round_text, calls)
            if note:
                shown = f"\n\n{note}" if round_text else note
                self._turn_out_chars += len(shown)
                yield TextDelta(text=shown)
                content.append({"type": "text", "text": note})

        # Normalize loose argument types against the declared schema before
        # the call reaches an executor (and before it enters history).
        for c in calls:
            c["input"] = _coerce_call_args(c["name"], c.get("input") or {}, props_by_tool)
            yield ToolCallStart(name=c["name"], id=c["id"])
            yield ToolCall(id=c["id"], name=c["name"], input=c["input"])

        if not calls and self._turn_out_chars == 0 and finish_reason != "length":
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
                "text. Try again, or switch model: some fine-tunes stall "
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
        # tool round. No usage (older builds): characters / 4 of the body
        # posted and of what came back (text, reasoning, tool-call arguments).
        usage = _summed_usage(spent) or {}
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
            )
        else:
            from server.costs.capture import estimated_counts, json_chars

            received = round_chars + reasoning_chars
            received += sum(json_chars(c.get("input") or {}) for c in calls)
            round_usage_ev = Usage(**estimated_counts(json_chars(payload), received))
        yield RoundResult(stop_reason=stop_reason, content=content, usage=round_usage_ev)

    def _output_limit_outcome(
        self, round_num: int, round_text: str, calls: list[dict]
    ) -> tuple[str, list[dict], str | None]:
        """How a round that hit the output cap (finish_reason ``length``) ends:
        ``(stop_reason, calls to keep, visible note or None)``.

        Cut off mid-answer, the round reports ``max_tokens`` so the engine
        shows it and continues the answer, as it does for cloud models. Cut
        off inside a tool call, the call's arguments are incomplete, so it is
        never run. Cut off before any text (a thinking model can spend the
        whole cap reasoning), a continuation would only reason again from
        scratch. Those two end the turn with a note naming the limit.
        """
        cap = self.max_tokens
        fix = f"raise max_output for {self.model_key} in chat_model_meta"
        if calls:
            names = ", ".join(sorted({c["name"] for c in calls}))
            log.warning(
                "Local turn (%s) hit the %d-token output cap inside a tool call (%s) at round %d.",
                self.model_key,
                cap,
                names,
                round_num,
            )
            note = (
                f"(Stopped: the model reached its output limit of {cap} tokens while "
                f"writing a call to {names}, so the call was not run. Ask for a "
                f"smaller step, or {fix}.)"
            )
            return "end_turn", [], note
        if not round_text:
            log.warning(
                "Local turn (%s) spent the %d-token output cap without an answer at round %d.",
                self.model_key,
                cap,
                round_num,
            )
            note = (
                f"(No answer: the model used its whole output limit of {cap} tokens "
                f"before writing a reply. Ask for something shorter, or {fix}.)"
            )
            return "end_turn", [], note
        return "max_tokens", [], None
