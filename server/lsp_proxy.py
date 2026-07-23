"""
LSP WebSocket Proxy — bridges browser ↔ language servers via stdio.

Each WebSocket connection spawns a language server subprocess and relays
JSON-RPC messages using LSP Content-Length framing on the stdio side and
plain JSON on the WebSocket side.

Supported languages:
  - Python  → pylsp (python-lsp-server)
  - JS/TS   → typescript-language-server --stdio

Endpoint:  ws://.../ws/lsp/{language}?workspace=/path/to/project
"""

import asyncio
import logging
import os
import shutil

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

log = logging.getLogger("whisper-studio")

router = APIRouter()

# Map language id → command to spawn
_SERVER_COMMANDS = {
    "python": {
        "cmd": ["pylsp"],
        "check": "pylsp",
    },
    "javascript": {
        "cmd": ["typescript-language-server", "--stdio"],
        "check": "typescript-language-server",
    },
    "typescript": {
        "cmd": ["typescript-language-server", "--stdio"],
        "check": "typescript-language-server",
    },
}


def _language_server_available(lang: str) -> bool:
    """Check if the language server binary is on PATH."""
    cfg = _SERVER_COMMANDS.get(lang)
    if not cfg:
        return False
    return shutil.which(cfg["check"]) is not None


async def _read_lsp_message(reader: asyncio.StreamReader) -> bytes | None:
    """Read one LSP message from stdio using Content-Length framing."""
    headers = b""
    while True:
        line = await reader.readline()
        if not line:
            return None  # EOF
        headers += line
        if line == b"\r\n" or line == b"\n":
            break

    # Parse Content-Length
    content_length = 0
    for h in headers.decode("ascii", errors="replace").split("\r\n"):
        if h.lower().startswith("content-length:"):
            content_length = int(h.split(":", 1)[1].strip())
            break

    if content_length == 0:
        return None

    body = await reader.readexactly(content_length)
    return body


def _encode_lsp_message(body: bytes) -> bytes:
    """Encode a message with LSP Content-Length header."""
    header = f"Content-Length: {len(body)}\r\n\r\n"
    return header.encode("ascii") + body


@router.websocket("/ws/lsp/{language}")
async def lsp_websocket_proxy(
    websocket: WebSocket,
    language: str,
    workspace: str = Query(default=""),
):
    """WebSocket ↔ Language Server stdio proxy."""
    # Reject cross-site WebSocket handshakes (the HTTP Origin middleware does
    # not see WS upgrades). Without this a malicious page could spawn and drive
    # a language server subprocess against the user's workspace.
    from server.infrastructure.security import is_ws_origin_allowed

    if not is_ws_origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008)
        return
    await websocket.accept()

    cfg = _SERVER_COMMANDS.get(language)
    if not cfg:
        await websocket.send_json({"error": f"Unsupported language: {language}"})
        await websocket.close(code=1008)
        return

    if not _language_server_available(language):
        await websocket.send_json(
            {"error": f"Language server not found for {language}. Install: {cfg['check']}"}
        )
        await websocket.close(code=1008)
        return

    # Resolve workspace path
    ws_path = workspace or os.getcwd()
    if not os.path.isdir(ws_path):
        ws_path = os.getcwd()

    log.info("LSP proxy: starting %s for %s (workspace=%s)", cfg["cmd"], language, ws_path)

    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *cfg["cmd"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=ws_path,
        )

        # Task: read from language server stdout → send to WebSocket
        async def server_to_client():
            try:
                while True:
                    body = await _read_lsp_message(process.stdout)
                    if body is None:
                        break
                    await websocket.send_text(body.decode("utf-8"))
            except (WebSocketDisconnect, asyncio.CancelledError):
                pass
            except Exception as e:
                log.warning("LSP server->client error: %s", e)

        # Task: read from WebSocket → write to language server stdin
        async def client_to_server():
            try:
                while True:
                    text = await websocket.receive_text()
                    encoded = _encode_lsp_message(text.encode("utf-8"))
                    process.stdin.write(encoded)
                    await process.stdin.drain()
            except (WebSocketDisconnect, asyncio.CancelledError):
                pass
            except Exception as e:
                log.warning("LSP client->server error: %s", e)

        # Task: log stderr from language server
        async def log_stderr():
            try:
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        break
                    log.debug(
                        "LSP stderr [%s]: %s",
                        language,
                        line.decode("utf-8", errors="replace").rstrip(),
                    )
            except asyncio.CancelledError:
                pass

        tasks = [
            asyncio.create_task(server_to_client()),
            asyncio.create_task(client_to_server()),
            asyncio.create_task(log_stderr()),
        ]

        # Wait for any task to complete (usually client disconnect)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except WebSocketDisconnect:
        log.info("LSP proxy: client disconnected (%s)", language)
    except Exception as e:
        log.error("LSP proxy error (%s): %s", language, e)
    finally:
        if process and process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        log.info("LSP proxy: session ended (%s)", language)
