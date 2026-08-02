"""llama-server protocol helpers for on-device turns.

The agentic loop itself lives in the unified turn engine
(server/chat/engine/runner.py + engine/local.py — the LocalAdapter); this
module keeps the llama-server specifics the adapter builds on: the
``/v1/chat/completions`` SSE parser, the tool-call accumulator, argument
coercion, tool-result message rendering, and the local post-turn memory hooks.

Speaking llama-server's OpenAI-compatible endpoint means upstream llama.cpp
does the jobs a hand-rolled in-process path had to solve per model family:

  * ``tool_calls`` arrive already parsed into OpenAI shape (name + JSON
    arguments), for every family upstream supports — so a new GGUF in config gets
    tool calling with no marker constants here.
  * reasoning arrives on ``reasoning_content``, so the thinking channel needs no
    marker splitter and can never leak raw markers into the answer.
  * real token counts arrive on ``usage`` (with ``stream_options.include_usage``),
    so the counter and context meter report measured tokens rather than the
    chars/4 guess a hand-rolled path had to make.

Because of that, this module contains NO model-specific tokens. There is no
fallback runtime: if llama-server cannot serve the model, the turn errors.
"""

from __future__ import annotations

import json
import logging

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
            # with only a `timings` object, and output falls back to the chars/4
            # estimate (see the fallback documented at the top of this module).
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
