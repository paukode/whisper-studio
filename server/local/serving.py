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
import os
import threading
import time

from server.local import llama_server, mlx_server

log = logging.getLogger("whisper-studio")

_ONESHOT_TIMEOUT = llama_server._ONESHOT_TIMEOUT

# How long a displacing load may wait for the busy turn(s) to finish before
# giving up with a clear error (a local turn can stream for minutes).
_BUSY_WAIT_TIMEOUT_S = int(os.environ.get("WHISPER_LOCAL_BUSY_WAIT_S", "600"))

# Live chat turns currently streaming from the resident server. A transition
# that would displace that server (different key, or a context-size change)
# WAITS for this to drain instead of stopping the server mid-stream — the bug
# where switching models in the composer killed the in-flight answer
# ("round 0 failed" with an empty error and a turn with no text).
_busy_lock = threading.Lock()
_busy_turns = 0


def begin_turn() -> None:
    """Mark a chat turn as streaming from the resident server. Callers must
    pair with ``end_turn`` in a finally. ``ensure_serving(mark_busy=True)``
    does this atomically with the residency check — prefer that."""
    global _busy_turns
    with _busy_lock:
        _busy_turns += 1


def end_turn() -> None:
    global _busy_turns
    with _busy_lock:
        _busy_turns = max(0, _busy_turns - 1)


def busy_turns() -> int:
    with _busy_lock:
        return _busy_turns


def _would_displace(key: str, n_ctx: int | None) -> bool:
    """Would serving ``key`` at ``n_ctx`` stop or restart what is resident?"""
    rk = resident_key()
    if rk is None:
        return False
    if rk != key:
        return True
    return n_ctx is not None and resident_n_ctx() != int(n_ctx)


# Serializes every cross-engine transition. Each engine's own _load_lock only
# protects loads WITHIN that engine; without this facade-level lock, two
# concurrent ensure_serving calls for different engines interleave their
# is_running checks and spawns, so either both engines end up resident (two
# multi-GB servers + the ASR stack in unified memory) or one call's stop()
# SIGTERMs the other call's freshly spawned, still-warming server. Held across
# validate → download → evict-other → delegated start, so the whole transition
# is atomic with respect to both engines. Read-only helpers and stop() stay
# lock-free, matching the per-engine modules.
_transition_lock = threading.Lock()


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


def ensure_serving(
    key: str,
    n_ctx: int | None = None,
    *,
    should_abort=None,
    mark_busy: bool = False,
) -> str:
    """Guarantee ``key`` is being served by its engine and return the base URL.

    One local model resident at a time is a CROSS-engine invariant, so the
    whole transition runs under the facade lock. Ordering mirrors what each
    engine already does internally: validate the engine and fetch the weights
    FIRST, and only then evict the other engine's resident model — a doomed
    request (mlx-lm missing, unknown key) or a minutes-long first download must
    never leave the machine with nothing resident. Blocking (a cold start loads
    gigabytes), so call it off the event loop.

    A transition that would displace the resident server (different key, or a
    context-size change) waits for live chat turns to finish first — stopping
    a server mid-stream kills the answer being written. ``should_abort`` lets
    a waiting caller bail out (the model-load endpoint passes its cancel
    flag); the wait gives up with a clear error after ``_BUSY_WAIT_TIMEOUT_S``.
    ``mark_busy=True`` atomically registers a chat turn on the returned server
    (pair with ``end_turn()`` in a finally), closing the gap where a load
    could sneak in between "URL returned" and "turn started".
    """
    from server.local import runtime

    # Fast path, lock-free: the key is already being served at the requested
    # size, which is every chat turn after the first. Without this, a live
    # turn on the resident model would queue behind another model's
    # minutes-long download holding the transition lock. Same exposure to a
    # concurrent stop as the engines' own lock-free fast paths.
    fast_target = mlx_server if engine_of(key) == "mlx" else llama_server
    if fast_target.resident_key() == key:
        base = fast_target.base_url()
        if base is not None and (n_ctx is None or fast_target.resident_n_ctx() == int(n_ctx)):
            if mark_busy:
                begin_turn()
            return base

    deadline = time.monotonic() + _BUSY_WAIT_TIMEOUT_S
    while True:
        # Wait (outside the lock) while live turns stream from a server this
        # transition would stop. Re-checked under the lock below, because a
        # new turn can start between the wait and the acquisition.
        while busy_turns() > 0 and _would_displace(key, n_ctx):
            if should_abort is not None and should_abort():
                raise RuntimeError("Model load cancelled.")
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"The resident model ({resident_key()}) is still answering; "
                    "try again when the current turn finishes."
                )
            time.sleep(0.25)

        with _transition_lock:
            if busy_turns() > 0 and _would_displace(key, n_ctx):
                continue  # a turn started while we waited — wait again
            if engine_of(key) == "mlx":
                target, other = mlx_server, llama_server
                mlx_server.ensure_available()
            else:
                target, other = llama_server, mlx_server
                llama_server.ensure_available()
            if key not in runtime.LOCAL_MODELS:
                raise RuntimeError(f"Unknown local model: {key}")
            # Idempotent (marker/file check), so the engine re-running it inside
            # its own ensure_serving is a cheap no-op.
            runtime.ensure_downloaded(key)
            if other.is_running():
                other.stop()
            url = target.ensure_serving(key, n_ctx)
            if mark_busy:
                begin_turn()
            return url


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
