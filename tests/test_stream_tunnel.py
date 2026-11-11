"""server/stream_tunnel.py: an HTTP request over a WebSocket runs through the
app as uvicorn would run it, streams its response back, and a closed socket
reaches the request as the client going away."""

import asyncio
import json
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.infrastructure.security import origin_guard
from server.stream_tunnel import router

PAGE = {"origin": "http://127.0.0.1:41500"}
# A full URL: a bare path makes the handshake Host "testserver", which the
# origin guard (rightly) refuses.
TUNNEL = "ws://127.0.0.1/ws/stream"


def _app(events: list[str]) -> FastAPI:
    app = FastAPI()
    app.middleware("http")(origin_guard)
    app.include_router(router)

    @app.post("/api/echo")
    async def echo(request: Request):
        return JSONResponse(
            {"got": await request.json(), "origin": request.headers.get("origin")},
            status_code=201,
        )

    @app.get("/api/stream")
    async def stream(n: int = 1000):
        async def gen():
            try:
                for i in range(n):
                    yield f"data: {i}\n\n"
                    await asyncio.sleep(0.01)
            finally:
                events.append("stream closed")

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def _client(events: list[str]) -> TestClient:
    return TestClient(_app(events), base_url="http://127.0.0.1")


def _send(ws, method: str, path: str, body: str = "", headers: dict | None = None, parts: int = 1):
    size = max(1, -(-len(body) // parts)) if body else 1
    chunks = [body[i : i + size] for i in range(0, len(body), size)]
    ws.send_text(
        json.dumps(
            {"method": method, "path": path, "headers": headers or {}, "chunks": len(chunks)}
        )
    )
    for chunk in chunks:
        ws.send_text(chunk)


def _body_until_close(ws) -> tuple[bytes, int]:
    data = b""
    while True:
        try:
            msg = ws.receive()
        except WebSocketDisconnect as e:
            return data, e.code
        if msg["type"] == "websocket.close":
            return data, msg.get("code", 1000)
        data += msg.get("bytes") or b""


def test_a_request_body_in_parts_gets_the_route_response():
    with _client([]).websocket_connect(TUNNEL, headers=PAGE) as ws:
        _send(
            ws,
            "POST",
            "/api/echo",
            json.dumps({"q": "sea" * 100}),
            {"content-type": "application/json"},
            parts=3,
        )
        head = ws.receive_json()
        body, code = _body_until_close(ws)

    assert head["status"] == 201
    assert ["content-type", "application/json"] in head["headers"]
    assert json.loads(body) == {"got": {"q": "sea" * 100}, "origin": PAGE["origin"]}
    assert code == 1000


def test_a_stream_arrives_whole_and_ends_with_a_normal_close():
    events: list[str] = []
    with _client(events).websocket_connect(TUNNEL, headers=PAGE) as ws:
        _send(ws, "GET", "/api/stream?n=3")
        head = ws.receive_json()
        body, code = _body_until_close(ws)

    assert head["status"] == 200
    assert body == b"data: 0\n\ndata: 1\n\ndata: 2\n\n"
    assert code == 1000


def test_a_closed_socket_ends_the_request_like_a_dropped_client():
    events: list[str] = []
    with _client(events).websocket_connect(TUNNEL, headers=PAGE) as ws:
        _send(ws, "GET", "/api/stream")
        ws.receive_json()
        ws.receive_bytes()
    deadline = time.monotonic() + 5
    while "stream closed" not in events and time.monotonic() < deadline:
        time.sleep(0.05)

    assert events == ["stream closed"]


def test_the_origin_comes_from_the_handshake_not_from_the_request_head():
    with _client([]).websocket_connect(TUNNEL, headers=PAGE) as ws:
        _send(
            ws,
            "POST",
            "/api/echo",
            "{}",
            {"content-type": "application/json", "origin": "https://evil.example"},
        )
        ws.receive_json()
        body, _ = _body_until_close(ws)

    assert json.loads(body)["origin"] == PAGE["origin"]


def test_a_cross_site_page_cannot_open_the_tunnel():
    with pytest.raises(WebSocketDisconnect) as closed:
        with _client([]).websocket_connect(
            "/ws/stream", headers={"origin": "https://evil.example"}
        ) as ws:
            ws.receive_json()
    assert closed.value.code == 1008


def test_only_api_paths_go_through():
    with _client([]).websocket_connect(TUNNEL, headers=PAGE) as ws:
        _send(ws, "GET", "/static/index.html")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 1008
