"""Engine dispatch for the on-device model servers.

Two engines serve local chat models — llama-server for GGUF weights and
``mlx_lm server`` for MLX snapshots — and both speak the same OpenAI-compatible
``/v1/chat/completions`` SSE, so everything above this module (LocalAdapter,
the stream parser, the turn engine) is engine-agnostic. This facade is the one
place that knows which engine a model key belongs to, and it owns the
cross-engine residency rule: ONE local model resident at a time across BOTH
engines, because chat weights and the ASR stack already share unified memory
on the memory-constrained on-device build (e.g. M3/18GB).

Callers that used to import llama_server directly (chat routes, engine
windows, main's lifecycle hooks, the models manager) go through here instead,
so adding a third engine stays a one-module change.
"""

from __future__ import annotations

import logging

from server.local import llama_server, mlx_server

log = logging.getLogger("whisper-studio")

_ONESHOT_TIMEOUT = llama_server._ONESHOT_TIMEOUT


def engine_of(key: str | None) -> str:
    """``"mlx"`` or ``"gguf"`` for a registry key (unknown keys read as gguf,
    matching the pre-MLX default for every existing entry)."""
    if not key:
        return "gguf"
    from server.local.runtime import LOCAL_MODELS

    return "mlx" if LOCAL_MODELS.get(key, {}).get("engine") == "mlx" else "gguf"


def wire_model(key: str) -> str:
    """The ``model`` field to send on the wire for this key.

    mlx_lm maps the ``default_model`` sentinel to its ``--model`` path and
    treats ANY other name as a new repo/path to load, so sending the registry
    key would make it try to download the key from the Hub. llama-server
    serves a single pinned model and only echoes the name back.
    """
    return mlx_server.WIRE_MODEL if engine_of(key) == "mlx" else key


def supports_tool_choice(key: str) -> bool:
    """Whether the engine honors OpenAI ``tool_choice``. mlx_lm ignores it, so
    the final-round "no more tools" contract must be enforced by omitting the
    tools list instead (see LocalAdapter)."""
    return engine_of(key) != "mlx"


def ensure_serving(key: str, n_ctx: int | None = None) -> str:
    """Guarantee ``key`` is being served by its engine and return the base URL.

    Stops the OTHER engine's resident model first — one local model at a time
    is a cross-engine invariant, not a per-engine one. Blocking (a cold start
    loads gigabytes), so call it off the event loop.
    """
    if engine_of(key) == "mlx":
        if llama_server.is_running():
            llama_server.stop()
        return mlx_server.ensure_serving(key, n_ctx)
    if mlx_server.is_running():
        mlx_server.stop()
    return llama_server.ensure_serving(key, n_ctx)


def stop() -> None:
    """Stop whichever engine is running. Safe when neither is."""
    llama_server.stop()
    mlx_server.stop()


def resident_key() -> str | None:
    """Key of the model currently served by either engine, or None."""
    return llama_server.resident_key() or mlx_server.resident_key()


def resident_n_ctx() -> int | None:
    """Context size of the resident model, or None. For GGUF this is the true
    ``--ctx-size`` window; for MLX it is the recorded request (informational)."""
    return llama_server.resident_n_ctx() or mlx_server.resident_n_ctx()


def reap_orphans() -> int:
    """Kill leftovers from BOTH engines after a hard kill of a previous run."""
    return llama_server.reap_orphans() + mlx_server.reap_orphans()


def complete(key: str, system_prompt: str, user: str, max_tokens: int = 1500) -> str:
    """One-shot, NON-streaming generation; returns the assistant text ('' on
    failure). Blocking — starts the model server if it isn't already serving
    ``key``, so call it off the event loop.

    The non-chat entry point (workspace-index extraction, the on-device session
    summariser), engine-agnostic: both servers answer the same non-streaming
    chat-completions request. Deliberately sends no tools — these callers want
    prose or JSON back, not a tool call.
    """
    import httpx

    try:
        url = ensure_serving(key)
    except Exception as e:
        log.warning("complete(%s): model server unavailable: %s", key, e)
        return ""
    try:
        r = httpx.post(
            f"{url}/v1/chat/completions",
            json={
                "model": wire_model(key),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "stream": False,
            },
            timeout=_ONESHOT_TIMEOUT,
        )
        r.raise_for_status()
        choices = (r.json() or {}).get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return llama_server._strip_reasoning_markers(msg.get("content") or "")
    except Exception as e:
        log.warning("complete(%s) failed: %s", key, e)
        return ""
