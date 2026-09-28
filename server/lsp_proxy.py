"""
LSP WebSocket proxy: bridges the browser's code editor to a language server over stdio.

Each WebSocket connection spawns a language server subprocess and relays
JSON-RPC messages using LSP Content-Length framing on the stdio side and
plain JSON on the WebSocket side.

The command comes from server/code_tools/commands.py::language_server:
  - Python: the app's own interpreter, ``python -P -m pylsp`` (safe path, so
    a workspace module never shadows pylsp's imports)
  - JS/TS:  typescript-language-server --stdio, found on PATH

When the server cannot start, or stops on its own, the proxy tells the editor
why with a JSON-RPC ``window/showMessage`` notification (type 1, Error) before
closing the socket, so the reason reaches the user instead of a bare grey dot.
The notification's params carry ``source: "whisper-studio"``, a field no
language server sends: a live server's own error popups are also
``window/showMessage`` errors, and the editor must not read them as the end of
the connection.

Endpoint:  ws://.../ws/lsp/{language}?workspace=/path/to/project
"""

import asyncio
import collections
import logging
import os

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from server.code_tools.commands import language_server

log = logging.getLogger("whisper-studio")

router = APIRouter()

# LSP MessageType.Error
_SHOW_MESSAGE_ERROR = 1
# Marks the proxy's own failure notification (src/hooks/lsp/failure.ts).
_FAILURE_SOURCE = "whisper-studio"
# WebSocket close code for "the server end hit a condition it could not handle".
_CLOSE_SERVER_FAILED = 1011
_STDERR_TAIL_LINES = 20


async def _fail(websocket: WebSocket, message: str) -> None:
    """Tell the editor why there is no language server, then close."""
    try:
        await websocket.send_json(
            {
                "jsonrpc": "2.0",
                "method": "window/showMessage",
                "params": {
                    "type": _SHOW_MESSAGE_ERROR,
                    "message": message,
                    "source": _FAILURE_SOURCE,
                },
            }
        )
        # A close reason is capped at 123 bytes; the full text went above.
        reason = message.encode("utf-8")[:120].decode("utf-8", errors="ignore")
        await websocket.close(code=_CLOSE_SERVER_FAILED, reason=reason)
    except Exception:  # noqa: BLE001 - the client may already be gone
        pass


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

    server = language_server(language)
    if server.argv is None:
        await _fail(websocket, server.reason)
        return

    # Resolve workspace path
    ws_path = workspace or os.getcwd()
    if not os.path.isdir(ws_path):
        ws_path = os.getcwd()

    log.info("LSP proxy: starting %s for %s (workspace=%s)", server.argv, language, ws_path)

    process = None
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                *server.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ws_path,
            )
        except OSError as e:
            await _fail(websocket, f"The {language} language server could not start: {e}")
            return

        # Task: read from language server stdout → send to WebSocket. Returns
        # True when the server closed its stdout (it is exiting).
        async def server_to_client() -> bool:
            try:
                while True:
                    body = await _read_lsp_message(process.stdout)
                    if body is None:
                        return True
                    await websocket.send_text(body.decode("utf-8"))
            except (WebSocketDisconnect, asyncio.CancelledError):
                pass
            except Exception as e:
                log.warning("LSP server->client error: %s", e)
            return False

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

        # Task: log stderr from language server, keeping the tail so a server
        # that dies can say why.
        stderr_tail: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL_LINES)

        async def log_stderr():
            try:
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip()
                    stderr_tail.append(text)
                    log.debug("LSP stderr [%s]: %s", language, text)
            except asyncio.CancelledError:
                pass

        server_task = asyncio.create_task(server_to_client())
        stderr_task = asyncio.create_task(log_stderr())
        tasks = [server_task, asyncio.create_task(client_to_server()), stderr_task]

        # Wait for any task to complete (usually client disconnect)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        # The server closed its stdout: it is exiting. Let stderr drain so the
        # reason is complete before the client is told.
        server_stopped = server_task in done and server_task.result()
        if server_stopped:
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
                await asyncio.wait_for(asyncio.shield(stderr_task), timeout=1)
            except asyncio.TimeoutError:
                pass

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # A clean exit follows the editor's own shutdown/exit; anything else is
        # a failure the user should see.
        if server_stopped and process.returncode not in (None, 0):
            detail = " ".join(line for line in stderr_tail if line)[-600:]
            message = f"The {language} language server stopped (exit status {process.returncode})."
            await _fail(websocket, f"{message} {detail}".strip())

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
