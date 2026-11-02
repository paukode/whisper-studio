"""Relay between the page and the capture helper of a source install.

The Mac app's window gives the page native mic and system/app audio capture
through WKWebView bridges (macapp/shell/NativeMicCapture.swift and
NativeAudioCapture.swift). A source install runs in a browser, so setup.sh
compiles the same capture code into a helper program
(vendor/capture/whisper-capture, built by macapp/helper/build_helper.sh) and
this module relays it: each WebSocket connection starts one helper and passes
JSON lines both ways. The page installs the same bridge globals the app window
injects (src/services/captureHelperBridge.ts), so its recording code does not
change.

One connection at a time: a second page gets an error instead of a second
helper, because two helpers would open the mic twice. The packaged app never
uses this (its window has the bridges), so the status route reports the
helper as unavailable there.
"""

import asyncio
import logging
import os

from fastapi import APIRouter, WebSocket

from server.infrastructure.paths import repo_root

log = logging.getLogger("whisper-studio")

router = APIRouter()

_busy = asyncio.Lock()


def helper_path() -> str:
    return os.path.join(repo_root(), "vendor", "capture", "whisper-capture")


def helper_available() -> bool:
    """True in a source install whose setup.sh built the helper."""
    if os.environ.get("WHISPER_HOME", "").strip():
        return False
    path = helper_path()
    return os.path.isfile(path) and os.access(path, os.X_OK)


@router.get("/api/capture-helper/status")
async def capture_helper_status() -> dict:
    return {"available": helper_available()}


def _error_lines(message: str) -> list[dict]:
    """The same error on both channels, so whichever source is recording sees it."""
    return [
        {"channel": "audio", "event": "onError", "args": [message]},
        {"channel": "mic", "event": "onError", "args": [message]},
    ]


@router.websocket("/ws/capture-helper")
async def capture_helper_socket(websocket: WebSocket) -> None:
    from server.infrastructure.security import is_ws_origin_present_and_allowed

    if not is_ws_origin_present_and_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    if not helper_available():
        for line in _error_lines("The capture helper is not installed. Run: bash setup.sh"):
            await websocket.send_json(line)
        await websocket.close()
        return
    if _busy.locked():
        for line in _error_lines(
            "Another Whisper Studio page is using the capture helper. Close that page and reload this one."
        ):
            await websocket.send_json(line)
        await websocket.close()
        return

    async with _busy:
        proc = await asyncio.create_subprocess_exec(
            helper_path(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=1 << 22,
        )
        log.info("capture helper started (pid %s)", proc.pid)

        async def page_to_helper() -> None:
            while True:
                text = await websocket.receive_text()
                proc.stdin.write(text.encode() + b"\n")
                await proc.stdin.drain()

        async def helper_to_page() -> None:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                await websocket.send_text(line.decode())

        to_helper = asyncio.create_task(page_to_helper())
        to_page = asyncio.create_task(helper_to_page())
        done, _ = await asyncio.wait({to_helper, to_page}, return_when=asyncio.FIRST_COMPLETED)
        helper_died = to_page in done
        to_helper.cancel()
        to_page.cancel()
        if proc.returncode is None:
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        log.info("capture helper exited (code %s)", proc.returncode)
        if helper_died:
            for line in _error_lines(f"The capture helper stopped (exit code {proc.returncode})."):
                try:
                    await websocket.send_json(line)
                except Exception:  # noqa: BLE001 - the page went away too
                    break
            await websocket.close()
