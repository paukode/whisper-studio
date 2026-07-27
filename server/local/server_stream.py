"""SSE adapter for on-device turns — the ONE on-device path.

Speaks llama-server's OpenAI-compatible ``/v1/chat/completions`` endpoint, which
means upstream llama.cpp does the two jobs a hand-rolled in-process path had to
solve per model family:

  * ``tool_calls`` arrive already parsed into OpenAI shape (name + JSON
    arguments), for every family upstream supports — so a new GGUF in config gets
    tool calling with no marker constants here.
  * reasoning arrives on ``reasoning_content``, so the thinking channel needs no
    marker splitter and can never leak raw markers into the answer.
  * real token counts arrive on ``usage`` (with ``stream_options.include_usage``),
    so the counter and context meter report measured tokens rather than the
    chars/4 guess a hand-rolled path had to make.

Because of that, this module contains NO model-specific tokens. It emits the same
event contract the frontend already consumes (``text`` / ``thinking_*`` /
``skill`` / ``skill_input`` / ``skill_result`` / ``approval_request`` / ``usage``
/ ``[DONE]``) and reuses the SAME executor + approval pipeline as the cloud paths
(``run_tool_round`` → ``execute_tool_batch`` + ``process_tool_results``), so the
safety gate is unchanged.

Parallel tool calls work here, matching how the cloud paths batch reads. There is
no fallback runtime: if llama-server cannot serve the model, the turn errors.
"""

from __future__ import annotations

import asyncio
import json
import logging

from server.local.tools import get_tool_schemas, run_tool_round
from server.utils import ndjson_dumps

log = logging.getLogger("whisper-studio")

_MAX_TOOL_ROUNDS = 50  # parity with the cloud + OpenAI paths

# Turns paused awaiting an approval continuation, keyed by session_id. In-memory
# and ephemeral, exactly like the cloud ``_paused_sessions`` — a restart drops it,
# and a continuation that finds no entry is handled as a fresh turn by the router.
_paused: dict[str, dict] = {}

_REQUEST_TIMEOUT = 900.0


def has_pause(session_id: str) -> bool:
    return session_id in _paused


# ── Post-turn memory hooks ───────────────────────────────────────────────────
# These used to live alongside the in-process streamer; they are model-agnostic
# bookkeeping, so they moved here with the single on-device path.


def _spawn_session_update(model_key: str, messages: list[dict], session_id: str) -> None:
    """Fire-and-forget on-device session-memory update after a local turn.

    The cadence + feature-flag gate lives in ``maybe_update_session_memory``;
    this just kicks it off (on the model's executor thread, via the local
    summariser) so session memory auto-builds fully offline. It runs one turn
    behind — the just-generated answer is captured on the next turn via the
    persisted history — which avoids threading the answer text out of the
    stream and keeps the model thread free while the response is still flushing.
    """
    if not messages:
        return
    try:
        from server.infrastructure.feature_flags import is_enabled

        if not is_enabled("session_memory"):
            return
        from server.infrastructure.async_tasks import spawn
        from server.local.runtime import local_model_meta
        from server.memory.session_memory import maybe_update_session_memory

        model_id = local_model_meta(model_key).get("id", "")
        spawn(
            maybe_update_session_memory(
                messages=list(messages),
                session_id=session_id,
                model_id=model_id,
            ),
            name="local-session-memory-update",
        )
    except Exception as e:  # never let memory bookkeeping disrupt a turn
        log.debug("Could not spawn local session update: %s", e)


def _spawn_extraction(
    model_key: str, messages: list[dict], session_id: str, ws_path: str | None
) -> None:
    """Fire-and-forget auto-memory extraction after a local turn.

    The extraction agent runs through the cloud agent runtime (run_agent
    resolves the memory_extractor model itself; the local model id passed here
    is ignored), so it only fires when cloud access is allowed: in "local"
    model mode the app is fully offline and extraction skips gracefully, while
    hybrid/cloud modes get the same two-tier extraction the cloud paths spawn
    (see chat/routes.py and openai_bedrock/stream.py). Throttle + cursor +
    feature-flag gates all live inside ``maybe_extract_memory``.
    """
    if not messages:
        return
    try:
        from server.infrastructure.feature_flags import is_enabled

        if not is_enabled("auto_memory"):
            return
        from server.infrastructure.model_mode import current_mode

        if current_mode() == "local":
            log.debug("Skipping auto-memory extraction: fully offline (model_mode=local)")
            return
        from server.infrastructure.async_tasks import spawn
        from server.local.runtime import local_model_meta
        from server.memory.extract import maybe_extract_memory

        model_id = local_model_meta(model_key).get("id", "")
        spawn(
            maybe_extract_memory(
                messages=list(messages),
                session_id=session_id,
                ws_path=ws_path,
                model_id=model_id,
            ),
            name="local-auto-memory-extract",
        )
    except Exception as e:  # never let memory bookkeeping disrupt a turn
        log.debug("Could not spawn local memory extraction: %s", e)


def _spawn_dream(model_key: str, ws_path: str | None) -> None:
    """Fire-and-forget dream consolidation after a local turn.

    Mirrors the cloud paths (chat/routes.py and openai_bedrock/stream.py):
    record the session against each memory tier and consolidate any tier that
    is due. The consolidator agent runs on a cloud model, so — like
    ``_spawn_extraction`` above — this skips gracefully when the app is fully
    offline (model_mode=local). Cadence + feature-flag gates also live inside
    ``record_and_maybe_dream``.
    """
    try:
        from server.infrastructure.feature_flags import is_enabled

        if not is_enabled("dream_consolidation"):
            return
        from server.infrastructure.model_mode import current_mode

        if current_mode() == "local":
            log.debug("Skipping dream consolidation: fully offline (model_mode=local)")
            return
        from server.infrastructure.async_tasks import spawn
        from server.local.runtime import local_model_meta
        from server.memory.dream import record_and_maybe_dream

        model_id = local_model_meta(model_key).get("id", "")
        spawn(
            record_and_maybe_dream(ws_path, model_id=model_id),
            name="local-dream-consolidation",
        )
    except Exception as e:  # never let memory bookkeeping disrupt a turn
        log.debug("Could not spawn local dream consolidation: %s", e)


def _spawn_memory_hooks(
    model_key: str, messages: list[dict], session_id: str, ws_path: str | None
) -> None:
    """Post-turn memory hooks for local turns, mirroring the cloud paths:
    session memory (summarised on-device, fully offline) + auto-memory
    extraction and dream consolidation (cloud agents; skipped when fully
    offline)."""
    _spawn_session_update(model_key, messages, session_id)
    _spawn_extraction(model_key, messages, session_id, ws_path)
    _spawn_dream(model_key, ws_path)


def _as_int(value) -> int:
    """Non-negative int from a usage field, 0 for anything unusable."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class _Tally:
    """Per-turn token accounting across tool rounds, from llama-server's usage.

    Semantics deliberately match the cloud path's usage frame so the same
    frontend counter reads correctly for both:

      * ``input_tokens`` is the NON-cached prefill for the round. llama.cpp
        reports the full prompt in ``prompt_tokens`` and the KV-reused prefix in
        ``prompt_tokens_details.cached_tokens``; the difference is what the model
        actually prefilled. Summing full prompts instead would count the whole
        conversation again on every tool round.
      * ``cache_read_tokens`` is that reused prefix (``--cache-reuse`` on the
        subprocess), the local analogue of a Bedrock cache read.
      * ``context_used`` is the full ``prompt_tokens`` of the LAST round — the
        real size of the conversation now in the window.

    When a build answers without usage (older llama-server ignoring
    ``include_usage``), output falls back to the previous chars/4 estimate and
    input stays 0 rather than the turn reporting nothing at all.
    """

    def __init__(self) -> None:
        self.round_input = 0
        self.round_output = 0
        self.total_input = 0
        self.total_output = 0
        self.round_cache_read = 0
        self.total_cache_read = 0
        self.context_used = 0

    def add_round(self, usage: dict | None, out_chars: int) -> None:
        usage = usage if isinstance(usage, dict) else {}
        prompt = _as_int(usage.get("prompt_tokens"))
        completion = _as_int(usage.get("completion_tokens"))
        details = usage.get("prompt_tokens_details")
        cached = _as_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
        if prompt or completion:
            # Clamp: a build that reports cached > prompt must not make input negative.
            cached = min(cached, prompt)
            self.round_input = prompt - cached
            self.round_cache_read = cached
            self.round_output = completion
            self.context_used = prompt
        else:
            log.debug("llama-server round returned no usage; estimating output from characters.")
            self.round_input = 0
            self.round_cache_read = 0
            self.round_output = out_chars // 4
        self.total_input += self.round_input
        self.total_output += self.round_output
        self.total_cache_read += self.round_cache_read


def _usage_line(model_key: str, tally: _Tally) -> str:
    """On-device turns are $0; the shape matches the cloud usage frame.

    ``context_used``/``context_max`` are omitted unless BOTH are known, so the
    status bar's meter never renders against a guessed denominator.
    """
    from server.local import llama_server

    payload: dict = {
        "input_tokens": tally.round_input,
        "output_tokens": tally.round_output,
        "total_input": tally.total_input,
        "total_output": tally.total_output,
        "cache_read_tokens": tally.round_cache_read,
        "total_cache_read": tally.total_cache_read,
        "estimated_cost_usd": 0.0,
        "model": model_key,
    }
    ctx_max = llama_server.resident_n_ctx()
    if tally.context_used and ctx_max:
        payload["context_used"] = tally.context_used
        payload["context_max"] = ctx_max
    return "data: " + ndjson_dumps({"usage": payload}) + "\n\n"


def _note_context(session_id: str, tally: _Tally) -> None:
    """Record the round's true prompt size against the session, the same way the
    cloud loop does, so the Stats panel's context bar (served from /api/costs)
    also reflects an on-device turn."""
    from server.local import llama_server

    ctx_max = llama_server.resident_n_ctx()
    if not (tally.context_used and ctx_max):
        return
    try:
        from server.chat.loop_hints import note_prompt_tokens

        note_prompt_tokens(session_id, tally.context_used, ctx_max)
    except Exception as e:  # never let bookkeeping disrupt a turn
        log.debug("Could not record local prompt tokens: %s", e)


def _to_openai_messages(system_prompt: str, messages: list[dict]) -> list[dict]:
    """Flatten the Bedrock-shaped history into OpenAI chat messages.

    Text-only: image blocks are dropped (vision is not wired here) and any
    tool/thinking blocks from a cloud turn are skipped, since a resumed local
    conversation carries its own tool history in OpenAI shape.
    """
    out: list[dict] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    for m in messages:
        content = m.get("content")
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
            out.append({"role": m.get("role", "user"), "content": text})
    return out


def _tool_results_to_messages(tool_results: list[dict], names_by_id: dict[str, str]) -> list[dict]:
    """Executed results → native OpenAI ``tool`` messages.

    The in-process path had to fold results into a plain user turn; here the
    template consumes them as real tool responses, which is a much stronger
    signal that this text is a tool result rather than the user talking.
    """
    msgs = []
    for r in tool_results:
        tid = r.get("tool_use_id", "")
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": tid,
                "name": names_by_id.get(tid, "tool"),
                "content": str(r.get("content", "")),
            }
        )
    return msgs


def _schema_props(schemas: list[dict]) -> dict[str, dict]:
    """``{tool_name: {param: json_schema}}`` from the declared OpenAI functions."""
    out: dict[str, dict] = {}
    for s in schemas or []:
        fn = s.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        props = ((fn.get("parameters") or {}).get("properties")) or {}
        if isinstance(props, dict):
            out[name] = props
    return out


def _coerce_arg(value, spec: dict):
    """Best-effort coercion of one argument to its declared JSON-schema type.

    Local models emit sloppier arguments than a hosted API does — a ``string``
    parameter can arrive as a list or a number. Bedrock validates against the
    schema before we ever see the call; llama-server hands through whatever the
    model produced, so an un-coerced list reaches an executor that calls
    ``.strip()`` on it and the tool call is wasted on an AttributeError.
    Only obvious mismatches are converted; anything already correct, or too
    ambiguous to convert, is passed through untouched for the executor to reject.
    """
    want = spec.get("type") if isinstance(spec, dict) else None
    if want == "string" and not isinstance(value, str):
        if isinstance(value, list):
            # "path": [] means "no argument" — it must become "" so the executor's
            # own `or "."` default applies. Left as a list it reaches .strip() and
            # the call dies on AttributeError (observed on a Gemma fine-tune).
            if not value:
                return ""
            # A single-element list is the common "path": ["a.py"] slip. Longer
            # lists are genuinely ambiguous, so join on comma only for scalars.
            flat = [v for v in value if isinstance(v, (str, int, float, bool))]
            if len(flat) == 1:
                return str(flat[0])
            if len(flat) == len(value):
                return ",".join(str(v) for v in flat)
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
        if value is None:
            return ""
        return value
    if want in ("integer", "number") and isinstance(value, str):
        try:
            return int(value) if want == "integer" else float(value)
        except ValueError:
            return value
    if want == "boolean" and isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "false"):
            return low == "true"
    if want == "array" and isinstance(value, (str, int, float, bool)):
        return [value]
    return value


def _coerce_call_args(name: str, args: dict, props_by_tool: dict[str, dict]) -> dict:
    """Coerce a parsed call's arguments toward the tool's declared types."""
    props = props_by_tool.get(name)
    if not props or not isinstance(args, dict):
        return args
    fixed = {}
    for key, value in args.items():
        spec = props.get(key)
        new = _coerce_arg(value, spec) if isinstance(spec, dict) else value
        if new is not value and type(new) is not type(value):
            log.info(
                "Coerced %s.%s from %s to %s (local models emit loose types).",
                name,
                key,
                type(value).__name__,
                type(new).__name__,
            )
        fixed[key] = new
    return fixed


class _CallAccumulator:
    """Reassembles streamed ``tool_calls`` deltas into complete calls.

    Arguments arrive as JSON string fragments across many deltas, and the index
    field is what ties fragments to their call when several stream in parallel.
    """

    def __init__(self) -> None:
        self._by_index: dict[int, dict] = {}

    def feed(self, deltas: list[dict]) -> None:
        for d in deltas or []:
            if not isinstance(d, dict):
                continue
            idx = d.get("index", 0) or 0
            slot = self._by_index.setdefault(idx, {"id": None, "name": None, "args": ""})
            if d.get("id"):
                slot["id"] = d["id"]
            fn = d.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["args"] += fn["arguments"]

    def finish(self) -> list[dict]:
        """``[{id, name, input}]`` in stream order, skipping nameless slots."""
        calls = []
        for idx in sorted(self._by_index):
            slot = self._by_index[idx]
            if not slot["name"]:
                continue
            raw = (slot["args"] or "").strip()
            try:
                args = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                log.warning(
                    "llama-server tool %r had unparseable arguments %r — passing {}.",
                    slot["name"],
                    raw[:200],
                )
                args = {}
            calls.append(
                {
                    "id": slot["id"] or f"call_{idx}",
                    "name": slot["name"],
                    "input": args if isinstance(args, dict) else {},
                }
            )
        return calls


async def _stream_round(base_url: str, payload: dict):
    """One generation round. Yields ``("text"|"thinking", piece)`` as it decodes,
    then ``("done", {"calls": [...], "finish_reason": str, "usage": {...}})``.

    Raises RuntimeError on transport/HTTP failure so the caller can surface it.
    """
    import httpx

    acc = _CallAccumulator()
    finish_reason = None
    usage: dict = {}
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        async with client.stream(
            "POST",
            f"{base_url}/v1/chat/completions",
            # include_usage is what makes llama-server append a final chunk
            # carrying prompt/completion token counts. WITHOUT it the stream ends
            # with only a `timings` object, which is why on-device turns used to
            # report 0 input tokens and a chars/4 guess for output.
            json={**payload, "stream": True, "stream_options": {"include_usage": True}},
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")
                raise RuntimeError(f"llama-server returned {resp.status_code}: {body[:500]}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if obj.get("error"):
                    raise RuntimeError(f"llama-server error: {str(obj['error'])[:400]}")
                # The usage chunk arrives last and carries an EMPTY choices list,
                # so it must be read before the delta handling below.
                if isinstance(obj.get("usage"), dict):
                    usage = obj["usage"]
                choices = obj.get("choices") or [{}]
                choice = choices[0] if choices else {}
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    yield ("thinking", delta["reasoning_content"])
                if delta.get("content"):
                    yield ("text", delta["content"])
                if delta.get("tool_calls"):
                    acc.feed(delta["tool_calls"])
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
    yield ("done", {"calls": acc.finish(), "finish_reason": finish_reason, "usage": usage})


async def _run_loop(
    model_key: str,
    base_url: str,
    convo: list[dict],
    schemas: list[dict],
    tool_ctx: dict,
    *,
    session_id: str,
    start_round: int = 0,
    memory_ctx: dict | None = None,
    tally: _Tally | None = None,
):
    """The agentic loop, shared by fresh turns and approval resumes.

    ``tally`` carries token totals across an approval pause so the counter keeps
    climbing on the resumed half of the turn instead of restarting at zero.
    """
    out_chars = 0
    thinking_on = bool(tool_ctx.get("thinking", False))
    props_by_tool = _schema_props(schemas)
    tally = tally or _Tally()

    for rnd in range(start_round, _MAX_TOOL_ROUNDS):
        payload: dict = {"model": model_key, "messages": convo, "max_tokens": 4096}
        if schemas:
            # Final-round parity with the cloud paths: forbid calls on the last
            # round so the model must synthesize an answer from what it has,
            # instead of hitting the cap with nothing to show the user.
            payload["tools"] = schemas
            payload["tool_choice"] = "none" if rnd == _MAX_TOOL_ROUNDS - 1 else "auto"
        # Some builds gate reasoning behind an explicit request; harmless when the
        # model has no thinking mode.
        if thinking_on:
            payload["chat_template_kwargs"] = {"enable_thinking": True}

        thinking_open = False
        calls: list[dict] = []
        round_usage: dict | None = None
        round_chars = 0
        try:
            async for kind, piece in _stream_round(base_url, payload):
                if kind == "thinking":
                    if not thinking_open:
                        thinking_open = True
                        yield f"data: {ndjson_dumps({'thinking_start': True})}\n\n"
                    yield f"data: {ndjson_dumps({'thinking': piece})}\n\n"
                elif kind == "text":
                    if thinking_open:
                        thinking_open = False
                        yield f"data: {ndjson_dumps({'thinking_stop': True})}\n\n"
                    out_chars += len(piece)
                    round_chars += len(piece)
                    yield f"data: {ndjson_dumps({'text': piece})}\n\n"
                else:  # ("done", {...})
                    calls = piece.get("calls") or []
                    round_usage = piece.get("usage")
        except Exception as e:
            if thinking_open:
                yield f"data: {ndjson_dumps({'thinking_stop': True})}\n\n"
            log.warning("llama-server round %d failed: %s", rnd, e)
            yield f"data: {ndjson_dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"
            return
        if thinking_open:  # reasoned but produced no answer text this round
            yield f"data: {ndjson_dumps({'thinking_stop': True})}\n\n"

        # One usage frame per round, like the cloud loop: the counter and the
        # context meter then update while a tool turn is still running, instead
        # of staying blank until the very last round.
        tally.add_round(round_usage, round_chars)
        _note_context(session_id, tally)
        yield _usage_line(model_key, tally)

        if not calls:
            if out_chars == 0:
                # The model ended the turn with neither text nor a tool call — it
                # can decode tokens that yield no content, no reasoning and no
                # parseable call (seen on some community fine-tunes after a tool
                # result). Say so instead of leaving an empty assistant bubble
                # that looks like the app broke. Not a substitute for a real
                # answer; it makes a model-quality failure legible.
                log.warning(
                    "Local turn (%s) ended with no text and no tool call at round %d.",
                    model_key,
                    rnd,
                )
                yield f"data: {ndjson_dumps({'text': '(No answer: the model ended its turn without producing any text. Try again, or switch model — some fine-tunes stall after a tool result.)'})}\n\n"
            yield "data: [DONE]\n\n"
            return

        # Normalize loose argument types against the declared schema before the
        # call reaches an executor (and before it goes into history).
        for c in calls:
            c["input"] = _coerce_call_args(c["name"], c["input"], props_by_tool)

        # Record the assistant's tool-calling turn in OpenAI shape so the template
        # renders it (and the follow-up tool messages) natively.
        convo.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c["input"])},
                    }
                    for c in calls
                ],
            }
        )

        tool_uses = [
            {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
            for c in calls
        ]
        names_by_id = {tu["id"]: tu["name"] for tu in tool_uses}
        for tu in tool_uses:
            yield f"data: {ndjson_dumps({'skill': tu['name']})}\n\n"
            yield f"data: {ndjson_dumps({'skill_input': tu['name'], 'input': tu['input']})}\n\n"

        tool_results, sse_events, has_pending_approval, has_user_question = await run_tool_round(
            tool_uses,
            session_id=session_id,
            plan_mode=tool_ctx.get("plan_mode", False),
            mode=tool_ctx.get("mode", "default"),
            session_approvals=tool_ctx.get("session_approvals"),
            session_denials=tool_ctx.get("session_denials"),
            config=tool_ctx.get("config"),
            transcript=tool_ctx.get("transcript", ""),
        )
        for ev in sse_events:
            yield f"data: {ev}\n\n"

        if has_pending_approval or has_user_question:
            # Hard stop: no destructive action has run, the handler only returned
            # a sentinel. Stash state so the approval continuation can fill in the
            # real result and resume from the next round.
            _paused[session_id] = {
                "model_key": model_key,
                "base_url": base_url,
                "convo": convo,
                "schemas": schemas,
                "tool_ctx": tool_ctx,
                "pending_results": tool_results,
                "names_by_id": names_by_id,
                "next_round": rnd + 1,
                "memory_ctx": memory_ctx,
                "tally": tally,
            }
            yield "data: [DONE]\n\n"
            return

        convo.extend(_tool_results_to_messages(tool_results, names_by_id))

    yield f"data: {ndjson_dumps({'text': '(Reached the local tool-call round limit without a final answer.)'})}\n\n"
    yield "data: [DONE]\n\n"


async def stream_chat(
    model_key: str,
    system_prompt: str,
    messages: list[dict],
    session_id: str,
    thinking: bool = False,
    tools: bool = False,
    tool_ctx: dict | None = None,
    ws_path: str | None = None,
    n_ctx: int | None = None,
):
    """Entry point for an on-device chat turn."""
    from server.local import llama_server

    tool_ctx = dict(tool_ctx or {})
    tool_ctx.setdefault("thinking", thinking)

    # Cold start loads gigabytes; keep it off the event loop.
    try:
        base_url = await asyncio.get_running_loop().run_in_executor(
            None, llama_server.ensure_serving, model_key, n_ctx
        )
    except Exception as e:
        log.warning("Could not start llama-server for %s: %s", model_key, e)
        yield f"data: {ndjson_dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"
        return

    schemas: list[dict] = []
    if tools:
        schemas, _ = get_tool_schemas(
            plan_mode=tool_ctx.get("plan_mode", False),
            ws_connected=tool_ctx.get("ws_connected", False),
            mcp_enabled_names=tool_ctx.get("mcp_enabled_names"),
            scope=tool_ctx.get("tool_scope", "core_web"),
            suppress_workspace_search=tool_ctx.get("suppress_ws_search", False),
        )

    convo = _to_openai_messages(system_prompt, messages)
    async for chunk in _run_loop(
        model_key,
        base_url,
        convo,
        schemas,
        tool_ctx,
        session_id=session_id,
        memory_ctx={"messages": messages, "ws_path": ws_path},
    ):
        yield chunk
    # Don't run memory hooks on a turn that paused for approval — it isn't
    # finished; the resume fires them at the true end of the turn.
    if not has_pause(session_id):
        _spawn_memory_hooks(model_key, messages, session_id, ws_path)


async def resume_chat(session_id: str, approved_tool_result, session_approvals: dict | None = None):
    """Resume a paused turn after the user approved / denied an action. The action
    already ran server-side via /api/approval/execute; here we only inject its
    result and continue. Falls back to a clean [DONE] when no paused state exists
    (e.g. after a restart)."""

    paused = _paused.pop(session_id, None)
    if not paused:
        log.warning("resume_chat: no paused state for %s — ending.", session_id)
        yield "data: [DONE]\n\n"
        return

    answers = (
        approved_tool_result if isinstance(approved_tool_result, list) else [approved_tool_result]
    )
    by_id = {a.get("tool_use_id", ""): a for a in answers if isinstance(a, dict)}

    tool_results = paused["pending_results"]
    names_by_id = paused["names_by_id"]
    for r in tool_results:
        ans = by_id.get(r.get("tool_use_id", ""))
        if ans is not None:
            r["content"] = ans.get("content", "")
            name = names_by_id.get(r["tool_use_id"], "tool")
            preview = str(r["content"])[:2000]
            yield f"data: {ndjson_dumps({'skill_result': name, 'output': preview})}\n\n"

    convo = paused["convo"]
    convo.extend(_tool_results_to_messages(tool_results, names_by_id))

    tool_ctx = paused["tool_ctx"]
    if session_approvals is not None:
        # Pick up any new "allow for session" choices for downstream rounds.
        tool_ctx = {**tool_ctx, "session_approvals": session_approvals}

    mem = paused.get("memory_ctx")
    async for chunk in _run_loop(
        paused["model_key"],
        paused["base_url"],
        convo,
        paused["schemas"],
        tool_ctx,
        session_id=session_id,
        start_round=paused["next_round"],
        memory_ctx=mem,
        tally=paused.get("tally"),
    ):
        yield chunk
    if mem and not has_pause(session_id):
        _spawn_memory_hooks(paused["model_key"], mem["messages"], session_id, mem["ws_path"])
