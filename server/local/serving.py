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
# Owners of the transitions past their busy check: a (re)start or eviction is
# about to stop or replace what is resident. While one runs, a new turn must
# not register on the resident server through the lock-free fast path (it
# would be stopped under it); it takes the slow path and queues behind the
# transition instead. Each entry is the claiming call's owner token, so
# stop_if_idle can tell the caller's own warming load from a turn that is
# about to register. Guarded by _busy_lock together with _busy_turns, so "no
# live turn" and "transition claimed" are one atomic step.
_claims: list[object] = []
# stop_if_idle calls between their check and the end of their stop. No
# transition claims and no turn registers while one runs, so nothing can pick
# up the server that is being stopped.
_stopping = 0
# The model the user switched away from while a turn still streamed from it
# (release_resident): stopped once the last turn ends, unless something asks
# for the local server again first (ensure_serving clears it). Guarded by
# _busy_lock.
_release_when_idle: str | None = None


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
        release = _busy_turns == 0 and _release_when_idle is not None
    if release:
        # The stop blocks for the engine's shutdown; the caller may be the
        # event loop, so it runs on its own thread.
        threading.Thread(target=_release_if_pending, name="local-release", daemon=True).start()


def release_resident() -> bool:
    """Free the resident model for a user who switched away from it. Stops it
    now when no turn streams from it and no transition is claimed; otherwise
    it is stopped when the last turn streaming from it ends, never under a
    live answer. Returns whether it is free now. Blocking: call it off the
    event loop."""
    global _release_when_idle
    key = resident_key()
    if key is None or stop_if_idle(key):
        return True
    with _busy_lock:
        _release_when_idle = key
        idle = _busy_turns == 0
    # The last turn may have ended between the refusal and the flag.
    return _release_if_pending() if idle else False


def _release_if_pending() -> bool:
    global _release_when_idle
    with _busy_lock:
        key = _release_when_idle
    if key is None:
        return False
    stopped = stop_if_idle(key)
    with _busy_lock:
        if _release_when_idle == key and (stopped or resident_key() != key):
            _release_when_idle = None
    return stopped


def _keep_resident() -> None:
    """Someone wants the local server again: cancel a pending release."""
    global _release_when_idle
    with _busy_lock:
        _release_when_idle = None


def busy_turns() -> int:
    with _busy_lock:
        return _busy_turns


def turns_besides(held: int) -> int:
    """Live turns on the resident server other than the ``held`` marks the
    caller accounts for (its own turn's, its own call's)."""
    return max(0, busy_turns() - held)


def _would_displace(key: str, n_ctx: int | None) -> bool:
    """Would serving ``key`` at ``n_ctx`` stop or restart what is resident?"""
    rk = resident_key()
    if rk is None:
        return False
    if rk != key:
        return True
    return n_ctx is not None and resident_n_ctx() != int(n_ctx)


def _held_off(key: str, n_ctx: int | None) -> bool:
    """A transition to ``key`` must wait: a live turn streams from a server it
    would displace, or a ``stop_if_idle`` is mid-stop. Hold ``_busy_lock``."""
    return _stopping > 0 or (_busy_turns > 0 and _would_displace(key, n_ctx))


def _must_wait(key: str, n_ctx: int | None) -> bool:
    with _busy_lock:
        return _held_off(key, n_ctx)


def _claim_transition(key: str, n_ctx: int | None, owner: object) -> bool:
    """Register ``owner``'s transition to ``key`` unless it is held off.
    Pair a True with ``_end_transition(owner)``."""
    with _busy_lock:
        if _held_off(key, n_ctx):
            return False
        _claims.append(owner)
        return True


def _end_transition(owner: object) -> None:
    with _busy_lock:
        _claims.remove(owner)


def _begin_turn_unless_transitioning() -> bool:
    """``begin_turn`` for the fast path, refused while a transition or a
    ``stop_if_idle`` runs."""
    global _busy_turns
    with _busy_lock:
        if _claims or _stopping:
            return False
        _busy_turns += 1
        return True


def _served_url(engine, key: str, n_ctx: int | None) -> str | None:
    """``engine``'s base URL when it serves ``key`` at ``n_ctx`` (any size
    when None), else None."""
    if engine.resident_key() != key:
        return None
    base = engine.base_url()
    if base is None or (n_ctx is not None and engine.resident_n_ctx() != int(n_ctx)):
        return None
    return base


# Serializes every cross-engine transition. Each engine's own _load_lock only
# protects loads WITHIN that engine; without this facade-level lock, two
# concurrent ensure_serving calls for different engines interleave their
# is_running checks and spawns, so either both engines end up resident (two
# multi-GB servers + the ASR stack in unified memory) or one call's stop()
# SIGTERMs the other call's freshly spawned, still-warming server. Held across
# the busy claim, evict-other and the delegated start, so the whole transition
# is atomic with respect to both engines (validation and the download run
# before it, see ensure_serving). Read-only helpers and stop() stay lock-free,
# matching the per-engine modules.
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
    started: threading.Event | None = None,
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
    flag); it is checked again once the transition lock is acquired, so a call
    cancelled while queued behind another transition claims nothing. The wait
    gives up with a clear error after ``_BUSY_WAIT_TIMEOUT_S``.
    ``mark_busy=True`` atomically registers a chat turn on the returned server
    (pair with ``end_turn()`` in a finally), closing the gap where a load
    could sneak in between "URL returned" and "turn started".
    ``started`` is set once this call is past the busy wait and about to
    (re)start a server, so a cancelling caller can stop what it started and
    nothing else: pass the same Event to ``stop_if_idle``.
    """
    from server.local import runtime

    _keep_resident()
    if engine_of(key) == "mlx":
        target, other = mlx_server, llama_server
    else:
        target, other = llama_server, mlx_server

    # Fast path, lock-free: the key is already being served at the requested
    # size, which is every chat turn after the first. Without this, a live
    # turn on the resident model would queue behind another model's
    # minutes-long download. A turn registers only while no transition runs,
    # and re-reads the server once registered: from then on no transition can
    # claim it, so what it streams from cannot be stopped under it.
    base = _served_url(target, key, n_ctx)
    if base is not None:
        if not mark_busy:
            return base
        if _begin_turn_unless_transitioning():
            base = _served_url(target, key, n_ctx)
            if base is not None:
                return base
            end_turn()

    # Validate and fetch the weights before waiting on anything. Both are
    # idempotent (the engine re-runs them as cheap no-ops), and doing the
    # possibly minutes-long download outside the transition lock means the
    # busy check below runs after it, right before anything is stopped.
    target.ensure_available()
    if key not in runtime.LOCAL_MODELS:
        raise RuntimeError(f"Unknown local model: {key}")
    runtime.ensure_downloaded(key)

    # The token stop_if_idle uses to tell this call's own transition apart.
    owner = started if started is not None else object()
    deadline = time.monotonic() + _BUSY_WAIT_TIMEOUT_S
    while True:
        # Wait (outside the lock) while live turns stream from a server this
        # transition would stop. Re-checked atomically by the claim below,
        # because a new turn can start between the wait and the acquisition.
        while _must_wait(key, n_ctx):
            if should_abort is not None and should_abort():
                raise RuntimeError("Model load cancelled.")
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"The resident model ({resident_key()}) is still answering; "
                    "try again when the current turn finishes."
                )
            time.sleep(0.25)

        with _transition_lock:
            # The lock can be held for a whole cold start of another model, and
            # a cancel that landed meanwhile had no wait loop to see it.
            if should_abort is not None and should_abort():
                raise RuntimeError("Model load cancelled.")
            if not _claim_transition(key, n_ctx, owner):
                continue  # a turn or a stop started while we waited, so wait again
            try:
                if started is not None and _served_url(target, key, n_ctx) is None:
                    started.set()
                if other.is_running():
                    other.stop()
                url = target.ensure_serving(key, n_ctx)
                if mark_busy:
                    begin_turn()
                return url
            finally:
                _end_transition(owner)


async def serve_turn(key: str, n_ctx: int | None = None, *, executor=None) -> str:
    """``ensure_serving(key, n_ctx, mark_busy=True)`` for an async turn.

    Returns the base URL with the turn registered; the caller pairs it with
    ``end_turn()`` in a finally. The load runs on ``executor`` (the loop's
    default when None) because a cold start blocks for minutes.

    Cancelling the awaiting task (Stop, a closed tab) cannot stop the worker
    thread, which still calls ``begin_turn`` when the load lands. A bare
    ``await`` would leave that increment with no decrement, and every later
    displacing load would wait out ``_BUSY_WAIT_TIMEOUT_S`` for a turn that no
    longer exists. So the load is shielded, and on cancel its eventual success
    releases the turn it registered. A cancel also aborts a load that is still
    waiting for another turn or queued behind another transition, so a stopped
    turn never displaces anything.
    """
    import asyncio
    import functools

    abort = threading.Event()
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(
        executor,
        functools.partial(ensure_serving, key, n_ctx, should_abort=abort.is_set, mark_busy=True),
    )
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        abort.set()
        fut.add_done_callback(_release_abandoned_turn)
        raise


def _release_abandoned_turn(fut) -> None:
    # Only a load that returned registered a turn; a failed or aborted one
    # raised before begin_turn.
    if not fut.cancelled() and fut.exception() is None:
        end_turn()


def stop_if_idle(key: str, started: threading.Event | None = None) -> bool:
    """Stop the resident server if it is ``key``, no chat turn streams from
    it, and no other transition is claimed. Returns whether it stopped.

    A claimed transition may be a turn's slow path that already holds the
    server's URL and is about to register on it, so the stop waits for none
    and simply refuses. The one exception is the caller's own load, named by
    the ``started`` Event it passed to ``ensure_serving``: stopping the server
    that load is still warming is how a cancel undoes it. Once the check
    passes, no transition claims and no turn registers until the stop is
    done, so nothing picks up the server being stopped."""
    global _stopping
    with _busy_lock:
        if _busy_turns > 0 or resident_key() != key:
            return False
        if any(owner is not started for owner in _claims):
            return False
        _stopping += 1
    try:
        stop()
    finally:
        with _busy_lock:
            _stopping -= 1
    return True


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


class ResidentCallError(RuntimeError):
    """A resident-only call could not run, or its answer is unusable.
    ``str(exc)`` is the user-facing reason."""


def _engine_for(key: str):
    return mlx_server if engine_of(key) == "mlx" else llama_server


def begin_resident_call(key: str) -> str:
    """Register a busy mark on the server already serving ``key`` and return
    its base URL. Pair a return with exactly one ``end_turn()`` in a finally.

    For a side call made from inside a live turn (the goal judge). Unlike
    ``ensure_serving`` it never loads, displaces or waits, and it leaves a
    pending ``release_resident`` alone. It raises ResidentCallError holding no
    mark when ``key`` is not the resident model or a transition is claimed,
    so a refusal must never be paired with ``end_turn``."""
    engine = _engine_for(key)
    if _served_url(engine, key, None) is None:
        raise ResidentCallError("the model is no longer loaded")
    if not _begin_turn_unless_transitioning():
        raise ResidentCallError("a model switch is in progress")
    base = _served_url(engine, key, None)
    if base is None:
        end_turn()
        raise ResidentCallError("the model is no longer loaded")
    return base


def _server_error_message(r) -> str:
    try:
        data = r.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            err = err.get("message") or err.get("type")
        msg = err or data.get("message") or data.get("detail")
        if msg:
            return str(msg)[:200]
    return (r.text or "").strip()[:200] or "no message"


def _resident_answer(r, max_tokens: int) -> str:
    if r.status_code >= 400:
        raise ResidentCallError(
            f"the model server answered HTTP {r.status_code}: {_server_error_message(r)}"
        )
    try:
        data = r.json()
    except ValueError:
        raise ResidentCallError("the model server's answer was not JSON") from None
    choices = (data.get("choices") if isinstance(data, dict) else None) or []
    if not choices or not isinstance(choices[0], dict):
        raise ResidentCallError("the model server returned no answer")
    if choices[0].get("finish_reason") == "length":
        raise ResidentCallError(f"the answer ran past its {max_tokens}-token budget")
    msg = choices[0].get("message") or {}
    text = llama_server._strip_reasoning_markers(msg.get("content") or "")
    if not text:
        if msg.get("reasoning_content"):
            raise ResidentCallError("the answer held only reasoning and was otherwise empty")
        raise ResidentCallError("the answer was empty")
    return text


async def complete_on_resident(
    key: str,
    system_prompt: str,
    user: str,
    *,
    max_tokens: int,
    json_schema: dict | None = None,
    timeout: float = _ONESHOT_TIMEOUT,
    held_marks: int = 0,
) -> str:
    """One non-streaming answer from the model already resident for ``key``.

    Resident-only (see ``begin_resident_call``): safe to call while the same
    turn holds its own busy mark, because it never enters ensure_serving's
    wait. Its own mark keeps the server from being stopped under the request.
    ``held_marks`` counts the marks the caller holds (1 from inside a turn).

    ``timeout`` bounds the answer, not the wait for the server: it answers
    one request at a time (llama-server runs ``--parallel 1``), so a request
    sent while another live turn holds a mark may queue behind that turn's
    round. Then the deadline grows by ``_BUSY_WAIT_TIMEOUT_S``, the bound a
    model switch waits for live turns, and a timeout says why.
    Deterministic and tool-free: temperature 0, thinking off for a thinking
    model, and on llama-server ``json_schema`` constrains the output (mlx_lm
    has no response_format, so the caller still validates what comes back).
    Async, so cancelling the awaiting task closes the connection and the
    finally releases the mark. Raises ResidentCallError with a reason on any
    failure; it never returns an empty string.
    """
    import asyncio

    import httpx

    from server.local.runtime import supports_thinking

    base = begin_resident_call(key)
    try:
        shared = turns_besides(held_marks + 1) > 0
        limit = timeout + (_BUSY_WAIT_TIMEOUT_S if shared else 0.0)
        body: dict = {
            "model": wire_model(key),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "stream": False,
            "temperature": 0,
        }
        if supports_thinking(key):
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if json_schema is not None and engine_of(key) == "gguf":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "answer", "strict": True, "schema": json_schema},
            }
        try:
            async with asyncio.timeout(limit):
                async with httpx.AsyncClient(timeout=limit) as client:
                    r = await client.post(f"{base}/v1/chat/completions", json=body)
        except (TimeoutError, httpx.TimeoutException):
            if shared:
                raise ResidentCallError(
                    f"no answer within {limit:.0f} s; the model was also answering "
                    "another chat or agent, and this request waited behind it"
                ) from None
            raise ResidentCallError(f"no answer within {timeout:.0f} s") from None
        except httpx.HTTPError as e:
            raise ResidentCallError(f"the model server could not be reached ({e})") from None
        return _resident_answer(r, max_tokens)
    finally:
        end_turn()


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
