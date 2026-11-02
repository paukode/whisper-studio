"""Model-server protocol helpers for on-device turns (llama-server and mlx_lm).

The agentic loop itself lives in the unified turn engine
(server/chat/engine/runner.py + engine/local.py — the LocalAdapter); this
module keeps the wire specifics the adapter builds on: the
``/v1/chat/completions`` SSE parser, the tool-call accumulator, argument
coercion, tool-result message rendering, and the local post-turn memory hooks.
Both engines speak the same OpenAI-compatible SSE, so one parser serves both
(the only dialect difference — the reasoning delta field name — is folded in
below).

Speaking the engines' OpenAI-compatible endpoint means they do the per-family
work:

  * ``tool_calls`` arrive already parsed into OpenAI shape (name + JSON
    arguments), for every family upstream supports — so a new GGUF in config gets
    tool calling with no marker constants here.
  * reasoning arrives on ``reasoning_content``, so the thinking channel needs no
    marker splitter and can never leak raw markers into the answer.
  * real token counts arrive on ``usage`` (with ``stream_options.include_usage``),
    so the counter and context meter report measured tokens.

Because of that, this module contains NO model-specific tokens. There is no
fallback runtime: if llama-server cannot serve the model, the turn errors.
"""

from __future__ import annotations

import json
import logging
import uuid

log = logging.getLogger("whisper-studio")

_REQUEST_TIMEOUT = 900.0


# ── Post-turn memory hooks ───────────────────────────────────────────────────
# Model-agnostic post-turn bookkeeping for the on-device path.


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
    except Exception as e:  # noqa: BLE001 (the turn already finished; say why memory is off)
        log.warning("Could not start the local session memory update: %s", e)


def _spawn_extraction(
    model_key: str, messages: list[dict], session_id: str, ws_path: str | None
) -> None:
    """Fire-and-forget auto-memory extraction after a local turn.

    The extraction agent runs on a cloud model (run_agent resolves the
    memory_extractor model itself; the local model id passed here is ignored),
    so it only runs when cloud access is allowed. Local mode records no
    memories, and says so: the auto_memory flag's ``local_mode_note`` is shown
    in the Feature flags panel and on the Memory row. Hybrid and Cloud modes
    get the same two-tier extraction the cloud paths spawn. The Local-mode,
    throttle, cursor and feature-flag gates all live inside
    ``maybe_extract_memory``.
    """
    if not messages:
        return
    try:
        from server.infrastructure.feature_flags import is_enabled

        if not is_enabled("auto_memory"):
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
    except Exception as e:  # noqa: BLE001 (the turn already finished; say why memory is off)
        log.warning("Could not start the local memory extraction: %s", e)


def _spawn_dream(ws_path: str | None) -> None:
    """Fire-and-forget dream consolidation after a local turn.

    Mirrors the cloud path (server/chat/engine/runner.py _fire_memory_hooks):
    record the session against each memory tier and consolidate any tier that
    is due. The consolidator agent runs on a cloud model, so the
    dream_consolidation flag is off in Local mode with a visible reason
    (``local_mode_off``). Cadence and feature-flag gates also live inside
    ``record_and_maybe_dream``.
    """
    try:
        from server.infrastructure.feature_flags import is_enabled

        # Off in Local mode through the flag itself (local_mode_off), which
        # the Feature flags panel shows with its reason.
        if not is_enabled("dream_consolidation"):
            return
        from server.infrastructure.async_tasks import spawn
        from server.memory.dream import record_and_maybe_dream

        spawn(record_and_maybe_dream(ws_path), name="local-dream-consolidation")
    except Exception as e:  # noqa: BLE001 (the turn already finished; say why memory is off)
        log.warning("Could not start the local dream consolidation: %s", e)


def _spawn_memory_hooks(
    model_key: str, messages: list[dict], session_id: str, ws_path: str | None
) -> None:
    """Post-turn memory hooks for local turns, mirroring the cloud paths:
    session memory (summarised on-device, fully offline) + auto-memory
    extraction and dream consolidation (cloud agents; skipped when fully
    offline)."""
    _spawn_session_update(model_key, messages, session_id)
    _spawn_extraction(model_key, messages, session_id, ws_path)
    _spawn_dream(ws_path)


def _tool_results_to_messages(tool_results: list[dict], names_by_id: dict[str, str]) -> list[dict]:
    """Executed results → native OpenAI ``tool`` messages.

    The template consumes them as real tool responses, a much stronger signal
    than a plain user turn that this text is a tool result rather than the
    user talking.
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


class _CallAccumulator:
    """Reassemble indexed calls, retaining errors until usage has arrived."""

    def __init__(self) -> None:
        self._by_index: dict[int, dict] = {}
        self.error: str | None = None
        self.seen = False

    @property
    def names(self) -> list[str]:
        return [c["name"] for c in self._by_index.values()] or (
            ["unknown tool"] if self.seen else []
        )

    @property
    def argument_chars(self) -> int:
        return sum(len(c["args"]) for c in self._by_index.values())

    def feed(self, deltas: list[dict]) -> None:
        self.seen = True
        if not isinstance(deltas, list):
            self.error = "Tool call deltas must be a list."
            return
        for d in deltas:
            if not isinstance(d, dict) or type(d.get("index")) is not int or d["index"] < 0:
                self.error = "Tool call has a missing or invalid index."
                continue
            idx = d["index"]
            fn = d.get("function")
            if not isinstance(fn, dict):
                self.error = "Tool call is missing its function."
                continue
            slot = self._by_index.get(idx)
            if slot is None:
                name = fn.get("name")
                if not isinstance(name, str) or not name.strip():
                    self.error = "Orphan tool arguments: no named call was started."
                    continue
                slot = {"id": None, "name": name, "args": ""}
                self._by_index[idx] = slot
            if "id" in d and d["id"] is not None:
                if not isinstance(d["id"], str) or not d["id"].strip():
                    self.error = "Tool call has an invalid id."
                elif slot["id"] not in (None, d["id"]):
                    self.error = "Conflicting tool call ids."
                else:
                    slot["id"] = d["id"]
            if "name" in fn and fn["name"] not in (None, slot["name"]):
                self.error = "Conflicting tool call names."
            if "arguments" in fn:
                if not isinstance(fn["arguments"], str):
                    self.error = "Tool arguments must be a JSON string."
                else:
                    slot["args"] += fn["arguments"]

    def finish(self) -> list[dict]:
        """Return complete objects only, refusing the whole invalid round."""
        from server.tool_inputs import ToolInputError, parse_tool_arguments, validate_call_ids

        if self.error:
            raise ToolInputError(self.error)
        calls = [
            {
                # Some local servers omit ids; keep the unique-id fallback.
                "id": slot["id"] or f"call_{uuid.uuid4().hex[:24]}",
                "name": slot["name"],
                "input": parse_tool_arguments(slot["args"], slot["name"]),
            }
            for _, slot in sorted(self._by_index.items())
        ]
        validate_call_ids(calls)
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
    completed = False
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        async with client.stream(
            "POST",
            f"{base_url}/v1/chat/completions",
            # include_usage is what makes the server append a final chunk
            # carrying prompt/completion token counts; without it the stream
            # ends with only a `timings` object and no counts.
            json={**payload, "stream": True, "stream_options": {"include_usage": True}},
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")
                raise RuntimeError(f"model server returned {resp.status_code}: {body[:500]}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    completed = True
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError as e:
                    # A dropped line can hold text or a tool-call delta, so the
                    # round fails instead of going on with a hole in it.
                    raise RuntimeError(
                        f"model server sent a stream line that is not JSON: {data[:200]!r}"
                    ) from e
                if obj.get("error"):
                    raise RuntimeError(f"model server error: {str(obj['error'])[:400]}")
                # The usage chunk arrives last and carries an EMPTY choices list,
                # so it must be read before the delta handling below.
                if isinstance(obj.get("usage"), dict):
                    usage = obj["usage"]
                choices = obj.get("choices") or [{}]
                choice = choices[0] if choices else {}
                delta = choice.get("delta") or {}
                # llama-server puts reasoning on `reasoning_content`; mlx_lm
                # server puts it on `reasoning`. Same channel either way.
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    yield ("thinking", reasoning)
                if delta.get("content"):
                    yield ("text", delta["content"])
                if delta.get("tool_calls"):
                    acc.feed(delta["tool_calls"])
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
    if not completed and finish_reason is None:
        # Both engines end a successful stream with a finish_reason chunk and
        # then [DONE]. mlx_lm's HTTP/1.0 body is close-delimited, so a
        # mid-generation death (e.g. Metal OOM) reads as a CLEAN EOF here —
        # without this guard a truncated answer would commit as a complete
        # round. (llama-server streams chunked HTTP/1.1, where an abrupt close
        # already raises inside aiter_lines.)
        raise RuntimeError(
            "model server closed the stream mid-generation (no finish_reason/[DONE])"
        )
    from server.tool_inputs import ToolInputError

    calls = []
    error = None
    if acc.seen and finish_reason != "tool_calls":
        error = f"Tool argument round did not finish normally ({finish_reason or 'missing finish reason'})."
    if error is None:
        try:
            calls = acc.finish()
        except ToolInputError as exc:
            error = str(exc)
    if error is None and finish_reason == "tool_calls" and not calls:
        error = "The model reported tool calls without a complete call payload."
    yield (
        "done",
        {
            "calls": calls,
            "finish_reason": finish_reason,
            "usage": usage,
            "argument_error": error,
            "call_names": acc.names,
            "argument_chars": acc.argument_chars,
        },
    )
