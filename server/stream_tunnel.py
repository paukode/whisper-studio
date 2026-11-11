"""HTTP requests over a WebSocket: the transport of the app's long requests.

The Mac app's web view (WKWebView) keeps at most 6 HTTP connections to the
app, and a 7th request waits until one is free (measured on 2026-11-10 with a
WKWebView probe; WebSockets do not count against the limit). Each running
chat reply, the session and git event streams, a model load and a /compact
held one for their whole life, so with 3 replies running only one connection
was left for every session load, save and upload.

The client (src/api/streamSocket.ts) opens one socket per request and sends
the request. This endpoint runs it through the app in-process, as uvicorn
would (the same routes, middleware and origin guard, ASGI spec 2.3), and sends
the response back. A closed socket is the client going away: the request then
receives http.disconnect, the same as when an HTTP client drops.

Protocol:
  client -> {"method", "path", "headers": {name: value}, "chunks": N},
            then N text frames that make up the body
  server -> {"status", "headers": [[name, value], ...]},
            then the body as binary frames, then a close with code 1000
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server.infrastructure.security import is_ws_origin_present_and_allowed

log = logging.getLogger("whisper-studio")
# One access line per tunnelled request, in uvicorn's own format (its access
# log never sees them), marked "HTTP/ws" so a log reader knows the transport.
_access = logging.getLogger("uvicorn.access")

router = APIRouter()

# Taken from the handshake, never from the client's request head: the origin
# guard reads them, and a script must not be able to choose them.
_FROM_HANDSHAKE = ("origin", "host", "cookie", "user-agent")


@router.websocket("/ws/stream")
async def stream_tunnel(ws: WebSocket) -> None:
    if not is_ws_origin_present_and_allowed(ws.headers.get("origin")):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        head = await ws.receive_json()
        parts = [await ws.receive_text() for _ in range(int(head.get("chunks") or 0))]
    except (WebSocketDisconnect, ValueError, TypeError, KeyError):
        return
    target = str(head.get("path") or "")
    if not target.startswith("/api/"):
        await ws.close(code=1008)
        return
    path, _, query = target.partition("?")
    body = "".join(parts).encode()

    headers = [
        (str(k).lower().encode("latin-1"), str(v).encode("latin-1"))
        for k, v in (head.get("headers") or {}).items()
        if str(k).lower() not in (*_FROM_HANDSHAKE, "content-length")
    ]
    for name in _FROM_HANDSHAKE:
        value = ws.headers.get(name)
        if value:
            headers.append((name.encode(), value.encode("latin-1")))
    headers.append((b"content-length", str(len(body)).encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": str(head.get("method") or "GET").upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": ws.scope.get("root_path", ""),
        "headers": headers,
        "client": ws.scope.get("client"),
        "server": ws.scope.get("server"),
        "state": ws.scope.get("state", {}),
    }

    gone = asyncio.Event()
    body_sent = False

    async def receive() -> dict:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        # Like uvicorn: a send to a client that left is dropped, not raised.
        if gone.is_set():
            return
        try:
            if message["type"] == "http.response.start":
                client = ws.scope.get("client")
                _access.info(
                    '%s - "%s %s HTTP/%s" %d',
                    f"{client[0]}:{client[1]}" if client else "-",
                    scope["method"],
                    target,
                    "ws",
                    message["status"],
                )
                await ws.send_json(
                    {
                        "status": message["status"],
                        "headers": [
                            [k.decode("latin-1"), v.decode("latin-1")]
                            for k, v in message.get("headers", [])
                        ],
                    }
                )
            elif message["type"] == "http.response.body" and message.get("body"):
                await ws.send_bytes(message["body"])
        except (WebSocketDisconnect, RuntimeError, OSError):
            gone.set()

    async def watch() -> None:
        try:
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass
        except (WebSocketDisconnect, RuntimeError):
            pass
        gone.set()

    watcher = asyncio.create_task(watch())
    try:
        await ws.scope["app"](scope, receive, send)
    except Exception:
        log.exception("Tunnelled request %s %s failed", scope["method"], path)
    finally:
        watcher.cancel()
        if not gone.is_set():
            try:
                await ws.close(code=1000)
            except RuntimeError:
                pass
