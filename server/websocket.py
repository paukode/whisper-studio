"""Bidirectional audio + control channel — thin ASR orchestrator.

One code path for every backend: the registry (server/asr) hands back a
module whose sessions turn PCM chunks into interim/final events, and this
handler does the same thing with those events regardless of which model
produced them. Speaker identification lives in server/diarization and is
applied here, on the orchestrator side, so backends stay completely
ignorant of each other and of diarization — deleting a backend is
deleting its file plus one registry line.

Message protocol to the client:
    {"type": "interim",  "text": str}
    {"type": "transcript", "text": str, "speaker": str, "chunk_id": int}
    {"type": "speaker_update", "updates": [{"chunk_id": int, "speaker": str}]}
    {"type": "pong"} / {"type": "session_ended"}

``speaker_update`` carries re-clustering corrections: diarization assigns
every utterance a label immediately (so transcripts never wait), then
periodically re-clusters everything seen so far and retro-fixes the few
utterances whose label changed.
"""

import asyncio
import json
import logging
import threading

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server import diarization
from server.asr import get_backend, resolve_name
from server.infrastructure.config import get as config_get

log = logging.getLogger("whisper-studio")

router = APIRouter(tags=["websocket"])


def resolve_translator(
    mode: str,
    backend_name: str,
    apple_available: bool,
    has_model_translate: bool,
    language: str | None,
    target: str,
) -> str | None:
    """Who produces this utterance's translation line: "model", "apple", None.

    Pure so it is unit-testable, and language-pair aware:
    - Whisper's translate head targets English only (99 sources -> en).
    - Canary is bidirectional with English as the hub (25 langs <-> en),
      never X -> Y with both non-English.
    - Apple handles any pair among its supported languages and auto-detects
      an unknown source; an unsupported pair or same-language input errors
      client-side and just clears the pending slot.
    "auto" prefers the engine's own decode when it can serve the pair,
    otherwise Apple. Explicit modes force one path and yield None when it
    cannot serve rather than silently substituting.
    """
    if mode == "off":
        return None
    if language is not None and language == target:
        return None

    from server.asr.canary_backend import CANARY_LANGUAGES

    def model_can() -> bool:
        if not has_model_translate:
            return False
        if backend_name == "whisper":
            return target == "en" and language is not None
        if backend_name == "canary":
            if language is None or target not in CANARY_LANGUAGES:
                return False
            return target == "en" or language == "en"
        return False

    if mode == "model":
        return "model" if model_can() else None
    if mode == "apple":
        return "apple" if apple_available else None
    # auto
    if model_can():
        return "model"
    return "apple" if apple_available else None


# ── Monotonic chunk-id resume across reconnects ─────────────────────────
# A finalized transcript chunk's id (the wire `chunk_id`) keys persisted
# speaker memory — SpeakerSession._embeddings / _assignments in
# server.diarization — and is echoed back in later `speaker_update`
# corrections. That speaker state survives a reconnect within the server's
# lifetime (it lives in diarization's RAM registry, keyed by session_id),
# so the id counter must survive too. The counter was a per-connection
# local starting at 0, so a reconnect for the same session (dropped mic,
# tab refresh, network blip) restarted at 0 and reused ids that already
# belonged to earlier chunks — overwriting their embeddings and
# mis-routing corrections to the wrong chunk.
#
# We keep the last-used counter here, keyed by session_id, and read it at
# connect so a reconnect resumes instead of colliding. Reset in lockstep
# with diarization.drop_session (explicit stop): a brand-new session then
# starts fresh at 0. Kept in this module (not on SpeakerSession) because
# dictation connections carry a chunk id but no speaker state, and this
# store must not depend on one existing.
_chunk_counters: dict[str, int] = {}
_chunk_counters_lock = threading.Lock()


def _next_chunk_start(session_id: str | None) -> int:
    """First chunk id a connection for this session should use: 0 for a
    brand-new session, or the next unused id if the session's speaker
    memory survived a reconnect. An id-less connection is always ephemeral
    (nobody else shares its state) and starts at 0."""
    if not session_id:
        return 0
    with _chunk_counters_lock:
        return _chunk_counters.get(session_id, 0)


def _record_chunk_counter(session_id: str | None, next_id: int) -> None:
    """Persist the next unused chunk id so a reconnect for this session
    resumes from it rather than restarting at 0. No-op without a session
    id (ephemeral connection)."""
    if not session_id:
        return
    with _chunk_counters_lock:
        _chunk_counters[session_id] = next_id


def _reset_chunk_counter(session_id: str | None) -> None:
    """Forget a session's chunk counter (explicit stop / session end), so a
    later session under the same id starts fresh at 0 — kept in lockstep
    with diarization.drop_session. No-op without a session id."""
    if not session_id:
        return
    with _chunk_counters_lock:
        _chunk_counters.pop(session_id, None)


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    session_id: str | None = None,
    backend: str | None = None,
    dictation: str | None = None,
):
    """``session_id`` is read from the query string (e.g. ``/ws?session_id=…``)
    so speaker labels survive reconnects within the server's lifetime. We
    use a plain Python default rather than ``fastapi.Query(None)`` because
    ``Query`` on a WebSocket endpoint can — depending on FastAPI/Starlette
    version — reject connections that omit the param entirely. Omitting it is
    a LIVE path, not legacy tolerance: dictation before any session exists
    connects with the bare ``/ws`` URL (see useChatInputMic's no-session
    fallback).
    """
    # Reject cross-site WebSocket handshakes (the HTTP Origin middleware does
    # not see WS upgrades). Prevents a malicious page from opening this audio
    # channel against the local app.
    from server.infrastructure.security import is_ws_origin_allowed

    if not is_ws_origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008)
        return
    await websocket.accept()

    loop = asyncio.get_event_loop()
    send_lock = asyncio.Lock()

    # Pick the ASR backend ONCE at connect so a mid-recording settings
    # change can't swap models on a live stream. A per-connection
    # ?backend= overrides the global config — the chat-input mic passes
    # backend=streaming so dictation always uses Parakeet even when the
    # meeting recorder is on Whisper. No param → follow the global config.
    backend_name = resolve_name((backend or "").strip() or config_get("transcription_backend"))
    backend_mod = get_backend(backend_name)
    # Lazily created on the first audio frame — building a session loads
    # and warms the backend's model, so an idle connection costs nothing.
    asr_session = None

    # Models load on demand with the progress banner (in every mode — a
    # silent multi-second first-sentence stall reads as broken), and a model
    # switch unloads the outgoing engine so the 1.6-3.1 GB checkpoints never
    # stack. Parakeet, warmed at startup when configured, is the exception —
    # see switch_backend.

    def _backend_label(name: str) -> str:
        return {"parakeet": "Parakeet", "canary": "Canary"}.get(resolve_name(name), "Whisper")

    # ?dictation=1 (the chat mic) skips speaker-ID: dictation is
    # single-user, so diarization is pointless and its compute is wasted.
    skip_speaker = (dictation or "").strip().lower() in ("1", "true", "yes")
    speakers = None if skip_speaker else diarization.get_session(session_id)
    # Chunk ids must stay monotonic across reconnects within a session (see
    # the _chunk_counters note above): resume from the highest id already
    # used for this session so a reconnect never reuses an id that keys an
    # earlier chunk's speaker state. A dictation connection carries a chunk
    # id but no speaker state, so it neither reads nor persists the shared
    # counter — it just starts at 0, and its ids never feed speaker memory.
    chunk_counter = 0 if skip_speaker else _next_chunk_start(session_id)
    # The user-provided participant count. Kept locally so it survives the
    # speaker-state reset on stop (the client only sends it on change).
    expected_speakers: int | None = None

    # Translate-to-English companion pass. translate_mode is seeded from
    # config so a pre-armed choice applies from the first utterance; the
    # panel's Translate dropdown updates it (and reports whether the Apple
    # on-device bridge exists on the client) via `set_translate` messages.
    # Model translations run as fire-and-forget tasks (decode queued on the
    # backend's executor BEHIND transcript decodes, so transcription latency
    # is untouched); Apple translations run on the client. The pending set
    # lets `stop` drain in-flight decodes so the last utterance's translation
    # isn't lost with the socket.
    translate_mode = str(config_get("translate_mode") or "off")
    translate_target = str(config_get("translate_target") or "en")
    apple_available = False
    pending_translations: set[asyncio.Task] = set()

    async def send_json(payload: dict) -> None:
        async with send_lock:
            try:
                await websocket.send_json(payload)
            except Exception:
                pass  # client closed mid-send — benign disconnect race

    async def emit_events(events: list[dict]) -> None:
        """Relay ordered backend events; label finals via diarization."""
        nonlocal chunk_counter
        for ev in events:
            text = ev.get("text", "")
            if not text:
                continue
            if ev["kind"] == "interim":
                await send_json({"type": "interim", "text": text})
                continue

            # final
            chunk_id = chunk_counter
            chunk_counter += 1
            # Persist the next unused id immediately (before the embed) so a
            # reconnect resumes past this chunk even if the embed fails and
            # no speaker-memory entry is written. Skipped for dictation,
            # whose ids don't feed speaker memory.
            if not skip_speaker:
                _record_chunk_counter(session_id, chunk_counter)
            speaker = "Speaker 1"
            if speakers is not None:
                embedding = await loop.run_in_executor(
                    diarization.executor, diarization.embed, ev.get("audio")
                )
                if embedding is None:
                    # Too short / failed embed: speaker continuity is the
                    # best guess and never pollutes the cluster space.
                    speaker = speakers.fallback_label()
                else:
                    duration = len(ev["audio"]) / 16000.0
                    speaker = speakers.assign(chunk_id, embedding, duration)
            # The engine's per-utterance language ID rides on the final
            # event. resolve_translator picks who produces the translation
            # line for the configured target — this server (engine decode,
            # scheduled below) or the client (Apple bridge, signalled by
            # translate_via). An UNKNOWN language (Parakeet does no language
            # ID) can still translate via Apple, whose on-device translator
            # auto-detects the source (and clears the pending slot on
            # same-language input), but never via a model decode.
            language = ev.get("language")
            translator = resolve_translator(
                translate_mode,
                backend_name,
                apple_available,
                hasattr(backend_mod, "translate_utterance"),
                language,
                translate_target,
            )
            payload = {
                "type": "transcript",
                "text": text,
                "speaker": speaker,
                "chunk_id": chunk_id,
                "language": language,
            }
            if translator is not None:
                # Tells the client to render a "Translating…" slot under the
                # segment until the translation for this chunk arrives.
                payload["translating"] = True
            if translator == "apple":
                payload["translate_via"] = "apple"
                payload["translate_target"] = translate_target
            await send_json(payload)
            if translator == "model":
                task = asyncio.create_task(
                    _run_translation(chunk_id, ev.get("audio"), language, translate_target)
                )
                pending_translations.add(task)
                task.add_done_callback(pending_translations.discard)
            if speakers is not None:
                # Periodic self-heal: re-cluster everything seen so far and
                # retro-fix the few utterances whose label changed.
                updates = await loop.run_in_executor(diarization.executor, speakers.maybe_recluster)
                if updates:
                    await send_json(
                        {
                            "type": "speaker_update",
                            "updates": [
                                {"chunk_id": cid, "speaker": label}
                                for cid, label in updates.items()
                            ],
                        }
                    )

    async def _run_translation(chunk_id: int, audio, language: str | None, target: str) -> None:
        """Decode the utterance's English translation and deliver it.

        Runs the decode on the backend's own single-worker executor, so it
        queues behind live transcript decodes (never ahead of them). An empty
        translation is still sent — the client uses it to clear the pending
        "Translating…" slot for this chunk.
        """
        try:
            translated = await loop.run_in_executor(
                backend_mod.executor, backend_mod.translate_utterance, audio, language, target
            )
        except Exception as e:
            log.warning("Translation task failed for chunk %s: %s", chunk_id, e)
            translated = ""
        await send_json(
            {"type": "translation", "chunk_id": chunk_id, "text": translated, "target": target}
        )

    async def load_model_into_memory() -> None:
        """Load the active backend's model into memory, streaming a progress
        banner to the client. The load itself is opaque (MLX from_pretrained /
        the first whisper decode give no byte progress), so the bar is a
        time-based ramp that completes the instant the load returns."""
        label = _backend_label(backend_name)
        await send_json(
            {
                "type": "model_loading",
                "backend": backend_name,
                "label": label,
                "stage": "start",
                "progress": 0.0,
            }
        )
        # Rough cold-load estimates; the bar fills toward 0.9 over this window
        # and snaps to 1.0 when the executor returns.
        resolved = resolve_name(backend_name)
        est_s = {"parakeet": 2.5, "canary": 3.0}.get(resolved, 9.0)
        done = asyncio.Event()

        async def ramp() -> None:
            p = 0.0
            try:
                while not done.is_set() and p < 0.9:
                    await asyncio.sleep(est_s / 18)
                    p = min(0.9, p + 0.05)
                    await send_json(
                        {
                            "type": "model_loading",
                            "backend": backend_name,
                            "label": label,
                            "stage": "loading",
                            "progress": round(p, 2),
                        }
                    )
            except asyncio.CancelledError:
                pass

        ramp_task = asyncio.create_task(ramp())
        try:
            await loop.run_in_executor(backend_mod.executor, backend_mod.load)
        finally:
            done.set()
            ramp_task.cancel()
            try:
                await ramp_task
            except Exception:
                pass
        await send_json(
            {
                "type": "model_loading",
                "backend": backend_name,
                "label": label,
                "stage": "ready",
                "progress": 1.0,
            }
        )

    async def ensure_session():
        nonlocal asr_session
        if asr_session is None:
            # Commit the model to memory first, with a banner, in EVERY mode —
            # a silent multi-second stall on the first sentence reads as
            # "nothing transcribes" (large-v3 and Canary load for seconds).
            if hasattr(backend_mod, "is_loaded") and not backend_mod.is_loaded():
                await load_model_into_memory()
            # Construct on the backend's own executor so model load and
            # warmup happen on the thread that will run every decode
            # (MLX streams are thread-local).
            asr_session = await loop.run_in_executor(
                backend_mod.executor, backend_mod.create_session
            )
        return asr_session

    async def finish_session() -> None:
        """Flush the in-flight utterance as a final and emit it."""
        nonlocal asr_session
        if asr_session is None:
            return
        try:
            events = await loop.run_in_executor(backend_mod.executor, asr_session.finish)
            await emit_events(events)
        except Exception as e:
            log.debug("ASR finish failed: %s", e)

    async def switch_backend(requested: str, variant: str | None = None) -> None:
        """Live model switch from the transcript panel header (or Settings).
        Flushes the outgoing pipeline first so the seam isn't clipped, then
        routes subsequent audio to the new model. No-op when unchanged.

        ``variant`` is accepted for wire compatibility with older clients
        (the whisper turbo/large-v3 variant era) and ignored."""
        del variant
        nonlocal backend_mod, backend_name, asr_session
        new_name = resolve_name(requested)
        if new_name == backend_name:
            return
        await finish_session()
        if asr_session is not None:
            try:
                await loop.run_in_executor(backend_mod.executor, asr_session.close)
            except Exception:
                pass
        asr_session = None
        # Free the outgoing engine's weights in every mode — whisper and
        # Canary are 2.4-3.1 GB each and stack up fast on small machines.
        # Parakeet (0.6B) stays resident: it is the dictation mic's always-on
        # engine and may be serving another connection.
        if (
            resolve_name(backend_name) != "parakeet"
            and hasattr(backend_mod, "unload")
            and backend_mod.is_loaded()
        ):
            old_label = _backend_label(backend_name)
            try:
                await loop.run_in_executor(backend_mod.executor, backend_mod.unload)
                await send_json(
                    {"type": "model_unloaded", "backend": backend_name, "label": old_label}
                )
            except Exception as e:
                log.debug("Unload of %s on switch failed: %s", backend_name, e)
        backend_name = new_name
        backend_mod = get_backend(new_name)
        log.info("Transcription model switched to %s", new_name)

    try:
        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            # Text frames: JSON control messages.
            if "text" in message and message["text"]:
                msg = json.loads(message["text"])

                if msg.get("type") == "ping":
                    await send_json({"type": "pong"})
                    continue

                if msg.get("type") == "set_model":
                    await switch_backend(msg.get("backend") or "", msg.get("variant"))
                    continue

                if msg.get("type") == "set_backend":
                    # Legacy client message (pre unified-model-selector).
                    await switch_backend(msg.get("backend") or "")
                    continue

                if msg.get("type") == "set_translate":
                    # Translate dropdown state + whether the client has the
                    # Apple on-device bridge. Sent at connect and on change;
                    # applies from the next final onward.
                    translate_mode = str(msg.get("mode") or "off")
                    translate_target = str(msg.get("target") or "en")
                    apple_available = bool(msg.get("apple_available"))
                    continue

                if msg.get("type") == "set_speakers":
                    # Participant-count hint from the transcript panel.
                    # 0 / null / absent = back to automatic estimation.
                    raw = msg.get("count")
                    expected_speakers = int(raw) if raw else None
                    if speakers is not None:
                        speakers.set_expected(expected_speakers)
                    continue

                if msg.get("type") == "stop":
                    # Drain whatever's in flight so the last sentence isn't
                    # clipped, then reset per-session state. The ack can race
                    # the client's close (the Header recorder closes the
                    # socket the instant it sends `stop`) — send_json guards.
                    await finish_session()
                    # Drain in-flight translations before ending the session:
                    # the client closes the socket after `session_ended`, and
                    # the last utterance's translation is usually still
                    # decoding at this point.
                    if pending_translations:
                        await asyncio.gather(*tuple(pending_translations), return_exceptions=True)
                    await send_json({"type": "session_ended"})
                    # Explicit user stop — drop speaker state so a later
                    # session under the same id starts fresh at Speaker 1
                    # instead of inheriting old speaker memory. Dictation
                    # connections never own speaker state, so they must not
                    # clear the meeting recorder's.
                    if not skip_speaker:
                        diarization.drop_session(session_id)
                        # Drop the chunk counter alongside the speaker state
                        # so the next session under this id starts fresh at 0
                        # rather than resuming the finished recording's ids.
                        _reset_chunk_counter(session_id)
                        speakers = diarization.get_session(session_id)
                        # The participant count outlives the speaker state:
                        # the meeting didn't change size because the user
                        # stopped and restarted the recorder.
                        speakers.set_expected(expected_speakers)
                    chunk_counter = 0
                    # Free the socket server-side instead of waiting on the
                    # client's close, which can race or fail and leave the
                    # connection (and one of the browser's ~6 per-host slots)
                    # held open. The recorder opens a fresh socket per take, so
                    # there is nothing more to receive on this one after stop.
                    try:
                        await websocket.close()
                    except Exception:
                        pass
                    return

            # Binary frames: raw PCM16 audio. Sequential by design — the
            # session's VAD buffer (and Parakeet's decoder) are stateful,
            # and decode is faster than real time, so inline awaiting keeps
            # ordering trivial without dropping frames.
            if "bytes" in message and message["bytes"]:
                raw_audio = message["bytes"]
                session = await ensure_session()
                events = await loop.run_in_executor(
                    backend_mod.executor, session.process, raw_audio
                )
                await emit_events(events)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        # A send racing the client's close ("Cannot call 'send' once a close
        # message has been sent") is a benign disconnect race — the client
        # closes the socket the instant it sends `stop`. Don't surface it as
        # an error; only genuine faults log loud.
        if "close message has been sent" in str(e):
            log.debug("WebSocket send raced client close: %s", e)
        else:
            log.error("WebSocket error: %s", e)
    finally:
        # Connection ended without an explicit ``stop`` (network blip, tab
        # close). Release decoder state; speaker labels stay in the RAM
        # registry so a reconnect under the same session id continues
        # where it left off. In-flight translations have no socket to land
        # on — cancel them rather than burn decode time on a dead client.
        for task in tuple(pending_translations):
            task.cancel()
        if asr_session is not None:
            try:
                await loop.run_in_executor(backend_mod.executor, asr_session.close)
            except Exception:
                pass
