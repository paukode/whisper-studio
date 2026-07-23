"""Live preview screencast — stream the agent's headless Chromium page to the
UI over a WebSocket via CDP ``Page.startScreencast``.

View-only: the frontend just renders the JPEG frames, so the user watches
exactly what the model's browser is doing (navigation, clicks, dark mode,
the overlay swallowing a click) without any input being forwarded back — so
it never interferes with the agent driving the page. Each connection opens
its own CDP session, so multiple viewers and the agent coexist.

Path is under ``/ws/...`` so Vite's dev proxy upgrades it as a WebSocket
(only ``/ws`` is proxied with ws:true; ``/api`` is plain HTTP).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server.preview.manager import preview_manager

log = logging.getLogger("whisper-studio")

router = APIRouter()

_MAX_WIDTH = 1280
_MAX_HEIGHT = 800
_JPEG_QUALITY = 60
_QUEUE_CAP = 2  # keep only the freshest frames — drop stale ones under backpressure


@router.websocket("/ws/preview/{name}/screencast")
async def preview_screencast(websocket: WebSocket, name: str):
    # Reject cross-site WebSocket handshakes (the HTTP Origin middleware does
    # not see WS upgrades). Without this a malicious page could read the live
    # screencast of the user's preview browser.
    from server.infrastructure.security import is_ws_origin_allowed

    if not is_ws_origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008)
        return
    session = preview_manager.get(name)
    if session is None:
        # 4404: app-level "no such preview session" (accept-then-close so the
        # browser gets a clean close rather than a handshake failure).
        await websocket.accept()
        await websocket.close(code=4404)
        return
    await websocket.accept()

    # The agent may not have navigated yet; ensure_started just creates the
    # page (blank) so the pane can attach and show frames the moment it loads.
    try:
        await session.browser.ensure_started()
    except Exception as e:  # noqa: BLE001
        log.warning("screencast: browser failed to start for %s: %s", name, e)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
        return

    page = session.browser.page
    loop = asyncio.get_running_loop()
    frames: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_CAP)

    try:
        cdp = await page.context.new_cdp_session(page)
    except Exception as e:  # noqa: BLE001
        log.warning("screencast: could not open CDP session for %s: %s", name, e)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
        return

    async def _ack(session_id: int) -> None:
        with contextlib.suppress(Exception):
            await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})

    def _on_frame(params: dict) -> None:
        # Chromium pauses the stream until each frame is acked, so ack every
        # frame immediately — even ones we drop from the display queue.
        sid = params.get("sessionId")
        if sid is not None:
            loop.create_task(_ack(sid))
        data = params.get("data")
        if not data:
            return
        if frames.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                frames.get_nowait()  # drop the oldest, keep latency low
        with contextlib.suppress(asyncio.QueueFull):
            frames.put_nowait(data)

    cdp.on("Page.screencastFrame", _on_frame)

    # Notice a client disconnect even when the page is static (no new frames):
    # a receive loop raises on close and flips the stop event.
    stop = asyncio.Event()

    async def _watch_close() -> None:
        with contextlib.suppress(Exception):
            while True:
                await websocket.receive_text()
        stop.set()

    watcher = asyncio.create_task(_watch_close())

    try:
        await cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": _JPEG_QUALITY,
                "maxWidth": _MAX_WIDTH,
                "maxHeight": _MAX_HEIGHT,
                "everyNthFrame": 1,
            },
        )
        log.info("screencast: streaming preview '%s'", name)
        while not stop.is_set():
            try:
                data = await asyncio.wait_for(frames.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue  # re-check stop, keep the socket warm
            await websocket.send_text(data)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.info("screencast: stream for '%s' ended: %s", name, e)
    finally:
        watcher.cancel()
        with contextlib.suppress(Exception):
            await cdp.send("Page.stopScreencast")
        with contextlib.suppress(Exception):
            await cdp.detach()
        with contextlib.suppress(Exception):
            await websocket.close()
