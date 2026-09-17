"""HTTP + WebSocket surface for voice mode.

  GET  /api/voice/status    is voice mode usable here, and with which model/voice
  PUT  /api/voice/settings  change voice_id / endpointing (persisted in config)
  WS   /ws/voice?session_id=...

The socket protocol, browser -> server:
  text  {"type":"start","history":[{role,content}],"model_key":"opus5.0","voice_id":"tiffany",
         "session_approvals":{...}}
        must be the first message; opens the Sonic stream
  bytes raw PCM16 mono 16 kHz microphone audio (frames of ~32 ms)
  text  {"type":"text","content":"..."}   a typed turn (cross-modal)
  text  {"type":"resolve","decision":"approve"|"deny"|"<answer>"}
        the user clicked the pending request card instead of speaking
  text  {"type":"end"}                     hang up; delegated runs still in flight keep
                                           going ("draining" event, then "ended" when done)
  text  {"type":"cancel"}                  stop the delegated runs as well (hang up if not yet)
  text  {"type":"ping"}

Server -> browser: the VoiceSession event dicts as JSON, except ``audio``,
which is sent as a binary frame of raw PCM16 mono 24 kHz.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket
from starlette.websockets import WebSocketDisconnect

from server.voice import protocol
from server.voice.sonic_client import SonicUnavailable, sdk_availability

log = logging.getLogger(__name__)

router = APIRouter()

VOICE_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "model_id": protocol.DEFAULT_MODEL_ID,
    # "" = follow bedrock_region. Nova 2 Sonic is in us-east-1, us-west-2,
    # eu-north-1 and ap-northeast-1 only.
    "region": "",
    "voice_id": protocol.DEFAULT_VOICE_ID,
    "endpointing": protocol.DEFAULT_ENDPOINTING,
}


def voice_settings() -> dict[str, Any]:
    """Effective voice settings: defaults, then the ``voice`` block of the
    merged config. ``region`` resolves to bedrock_region when blank."""
    from server.infrastructure.config import load_config

    cfg = load_config()
    merged = dict(VOICE_DEFAULTS)
    block = cfg.get("voice")
    if isinstance(block, dict):
        merged.update({k: v for k, v in block.items() if k in VOICE_DEFAULTS})
    if not str(merged.get("region") or "").strip():
        merged["region"] = str(cfg.get("bedrock_region") or "us-east-1")
    if merged.get("voice_id") not in protocol.VOICE_IDS:
        merged["voice_id"] = protocol.DEFAULT_VOICE_ID
    if str(merged.get("endpointing", "")).upper() not in protocol.ENDPOINTING_LEVELS:
        merged["endpointing"] = protocol.DEFAULT_ENDPOINTING
    return merged


def _credentials_present() -> bool:
    try:
        import botocore.session

        return botocore.session.get_session().get_credentials() is not None
    except Exception:  # noqa: BLE001
        return False


@router.get("/api/voice/status")
async def voice_status() -> dict[str, Any]:
    settings = voice_settings()
    available, reason = sdk_availability()
    if available and not settings.get("enabled", True):
        available, reason = False, "Voice mode is disabled in config (voice.enabled)."
    if available and not await asyncio.get_running_loop().run_in_executor(
        None, _credentials_present
    ):
        available, reason = (
            False,
            ("No AWS credentials found. Voice mode uses the same credentials as chat."),
        )
    return {
        "available": available,
        "reason": reason,
        "model_id": settings["model_id"],
        "region": settings["region"],
        "voice_id": settings["voice_id"],
        "endpointing": settings["endpointing"],
        "voices": list(protocol.VOICES),
    }


@router.put("/api/voice/settings")
async def update_voice_settings(request: Request) -> dict[str, Any]:
    from server.infrastructure.config import _load_user_config, save_config

    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be an object")
    update: dict[str, Any] = {}
    if "voice_id" in body:
        if body["voice_id"] not in protocol.VOICE_IDS:
            raise HTTPException(400, f"unknown voice_id {body['voice_id']!r}")
        update["voice_id"] = body["voice_id"]
    if "endpointing" in body:
        level = str(body["endpointing"]).upper()
        if level not in protocol.ENDPOINTING_LEVELS:
            raise HTTPException(400, "endpointing must be LOW, MEDIUM or HIGH")
        update["endpointing"] = level
    if "enabled" in body:
        update["enabled"] = bool(body["enabled"])
    if not update:
        raise HTTPException(400, "nothing to update")
    raw = _load_user_config()
    block = raw.get("voice") if isinstance(raw.get("voice"), dict) else {}
    raw["voice"] = {**block, **update}
    save_config(raw)
    return {"updated": True, **voice_settings()}


def _workspace_name() -> str | None:
    try:
        from server.workspace import get_workspace_path

        path = get_workspace_path()
        return path.rstrip("/").split("/")[-1] if path else None
    except Exception:  # noqa: BLE001
        return None


@router.websocket("/ws/voice")
async def voice_websocket(websocket: WebSocket, session_id: str | None = None) -> None:
    from server.infrastructure.security import is_ws_origin_allowed
    from server.voice.session import VoiceConfig, VoiceSession, build_system_prompt

    if not is_ws_origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    send_lock = asyncio.Lock()

    async def emit(ev: dict[str, Any]) -> None:
        async with send_lock:
            try:
                if ev.get("type") == "audio":
                    await websocket.send_bytes(ev["pcm"])
                else:
                    await websocket.send_json(ev)
            except Exception:  # noqa: BLE001 - client went away mid-send
                pass

    # First message: start. Anything else is a protocol error.
    try:
        first = await websocket.receive_json()
    except (WebSocketDisconnect, ValueError, KeyError, RuntimeError):
        # KeyError: a binary (audio) frame arrived before the start message.
        return
    if not isinstance(first, dict) or first.get("type") != "start":
        await emit({"type": "error", "message": "first message must be start"})
        await websocket.close(code=1002)
        return

    settings = voice_settings()
    if not settings.get("enabled", True):
        await emit(
            {"type": "error", "message": "Voice mode is disabled in config (voice.enabled)."}
        )
        await emit({"type": "ended", "reason": "unavailable"})
        await websocket.close()
        return
    voice_id = first.get("voice_id") if first.get("voice_id") in protocol.VOICE_IDS else None
    config = VoiceConfig(
        model_id=str(settings["model_id"]),
        region=str(settings["region"]),
        voice_id=str(voice_id or settings["voice_id"]),
        endpointing=str(settings["endpointing"]),
        system_prompt=build_system_prompt(workspace_name=_workspace_name()),
    )
    history = first.get("history") if isinstance(first.get("history"), list) else []
    session = VoiceSession(
        session_id=str(session_id or first.get("session_id") or "voice"),
        config=config,
        emit=emit,
        history=[r for r in history if isinstance(r, dict)],
        model_key=str(first["model_key"]) if first.get("model_key") else None,
        session_approvals=(
            first["session_approvals"] if isinstance(first.get("session_approvals"), dict) else None
        ),
    )
    try:
        await session.start()
    except SonicUnavailable as exc:
        await emit({"type": "error", "message": str(exc)})
        await emit({"type": "ended", "reason": "unavailable"})
        await websocket.close()
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("voice: could not open the Sonic stream")
        await emit({"type": "error", "message": f"Could not start voice mode: {exc}"})
        await emit({"type": "ended", "reason": "error"})
        await websocket.close()
        return

    hangups: set[asyncio.Task] = set()

    async def client_loop() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                if msg.get("bytes"):
                    await session.send_audio(msg["bytes"])
                    continue
                text = msg.get("text")
                if not text:
                    continue
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                kind = data.get("type") if isinstance(data, dict) else None
                if kind == "text":
                    await session.send_text(str(data.get("content") or ""))
                elif kind == "resolve":
                    session.resolve_pending_from_ui(str(data.get("decision") or ""))
                elif kind == "end":
                    # Hang up, but keep reading: while delegated runs drain,
                    # their request cards are still answered over this socket.
                    task = asyncio.create_task(session.stop("user"))
                    hangups.add(task)
                    task.add_done_callback(hangups.discard)
                elif kind == "cancel":
                    # Stop the background work too (the chat's Stop while
                    # voice runs are still going after a hang-up).
                    task = asyncio.create_task(session.stop("user", cancel_runs=True))
                    hangups.add(task)
                    task.add_done_callback(hangups.discard)
                elif kind == "ping":
                    await emit({"type": "pong"})
        except (WebSocketDisconnect, RuntimeError):
            return

    client_task = asyncio.create_task(client_loop())
    done_task = asyncio.create_task(session.done.wait())
    try:
        await asyncio.wait({client_task, done_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (client_task, done_task):
            if not task.done():
                task.cancel()
        # The browser is gone (or the session finished on its own): nothing can
        # receive a background run's answer any more, so runs are cancelled.
        await session.stop("user", cancel_runs=True)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
