"""Voice routes: status reporting, settings validation, and the /ws/voice
handshake (origin guard, start message, audio + text forwarding) against a
fake Sonic stream."""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.voice import routes as voice_routes
from server.voice import session as voice_session
from server.voice.routes import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, base_url="http://localhost")


def test_status_reports_unavailable_reason(monkeypatch):
    monkeypatch.setattr(voice_routes, "sdk_availability", lambda: (False, "no sdk"))
    r = _client().get("/api/voice/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False and body["reason"] == "no sdk"
    assert body["model_id"] == "amazon.nova-2-sonic-v1:0"
    assert any(v["id"] == "tiffany" for v in body["voices"])


def test_status_available_when_sdk_and_credentials(monkeypatch):
    monkeypatch.setattr(voice_routes, "sdk_availability", lambda: (True, None))
    monkeypatch.setattr(voice_routes, "_credentials_present", lambda: True)
    body = _client().get("/api/voice/status").json()
    assert body["available"] is True and body["reason"] is None
    assert body["region"]  # resolved from bedrock_region when voice.region is blank


def test_status_disabled_by_config(monkeypatch):
    monkeypatch.setattr(voice_routes, "sdk_availability", lambda: (True, None))
    monkeypatch.setattr(
        voice_routes,
        "voice_settings",
        lambda: {**voice_routes.VOICE_DEFAULTS, "enabled": False, "region": "us-east-1"},
    )
    body = _client().get("/api/voice/status").json()
    assert body["available"] is False and "disabled" in body["reason"]


def test_settings_validation(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        "server.infrastructure.config._load_user_config", lambda: {"voice": {"voice_id": "amy"}}
    )
    monkeypatch.setattr("server.infrastructure.config.save_config", lambda raw: saved.update(raw))
    c = _client()
    assert c.put("/api/voice/settings", json={"voice_id": "nobody"}).status_code == 400
    assert c.put("/api/voice/settings", json={"endpointing": "sometimes"}).status_code == 400
    assert c.put("/api/voice/settings", json={}).status_code == 400
    r = c.put("/api/voice/settings", json={"voice_id": "matthew", "endpointing": "high"})
    assert r.status_code == 200
    assert saved["voice"] == {"voice_id": "matthew", "endpointing": "HIGH"}


def test_ws_rejects_cross_site_origin():
    c = _client()
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/voice", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_text()


def test_ws_requires_start_first():
    c = _client()
    with c.websocket_connect("/ws/voice?session_id=s") as ws:
        ws.send_json({"type": "text", "content": "hi"})
        msg = ws.receive_json()
        assert msg["type"] == "error" and "start" in msg["message"]


class _FakeStream:
    def __init__(self):
        self.sent = []
        self.q: asyncio.Queue = asyncio.Queue()

    async def send(self, event_json):
        self.sent.append(json.loads(event_json)["event"])

    async def receive(self):
        return await self.q.get()

    async def close(self):
        await self.q.put(None)


def test_ws_start_forwards_audio_and_text_and_ends(monkeypatch):
    streams = []

    async def opener(model_id, region):
        s = _FakeStream()
        streams.append(s)
        return s

    monkeypatch.setattr(voice_session, "open_stream", opener)

    c = _client()
    with c.websocket_connect("/ws/voice?session_id=abc") as ws:
        ws.send_json(
            {
                "type": "start",
                "history": [{"role": "user", "content": "earlier"}],
                "model_key": "opus5.0",
                "voice_id": "amy",
            }
        )
        first = ws.receive_json()
        assert first["type"] == "ready" and first["voice_id"] == "amy"
        state = ws.receive_json()
        assert state == {"type": "state", "state": "listening"}
        ws.send_bytes(b"\x00\x00\x10\x00")
        ws.send_json({"type": "text", "content": "and this typed"})
        typed = ws.receive_json()
        assert typed == {"type": "user_transcript", "text": "and this typed", "typed": True}
        ws.send_json({"type": "end"})
        ended = ws.receive_json()
        assert ended == {"type": "ended", "reason": "user"}
    s = streams[0]
    kinds = [next(iter(e)) for e in s.sent]
    assert kinds[0] == "sessionStart"
    assert any("audioInput" in e for e in s.sent)
    seeded = [e["textInput"]["content"] for e in s.sent if "textInput" in e]
    assert "earlier" in seeded and "and this typed" in seeded
    assert kinds[-1] == "sessionEnd"


def test_ws_cancel_stops_background_runs_after_a_hang_up(monkeypatch):
    """End keeps delegated runs going (draining); cancel is the chat's Stop for
    that work: the run is cancelled and the session ends."""
    import asyncio

    streams = []

    async def opener(model_id, region):
        s = _FakeStream()
        streams.append(s)
        return s

    monkeypatch.setattr(voice_session, "open_stream", opener)
    cancelled = {"hit": False}

    async def slow_tool(name, tool_input, ctx):
        async def run():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled["hit"] = True
                raise

        task = asyncio.create_task(run())
        ctx.runs["r1"] = {"request": "long job", "task": task, "detached": True}
        ctx.register_task(task)
        return "Working on it: long job."

    monkeypatch.setattr(voice_session, "run_tool", slow_tool)

    class _ToolStream(_FakeStream):
        # Sonic answers the typed "go" with a tool call, queued from the server
        # loop where the stream lives (the test thread must not touch it).
        async def send(self, event_json):
            await super().send(event_json)
            ev = json.loads(event_json)["event"]
            if "textInput" in ev and ev["textInput"]["content"] == "go":
                tool_use = {
                    "event": {
                        "toolUse": {
                            "toolUseId": "tu-1",
                            "toolName": "ask_assistant",
                            "content": "{}",
                        }
                    }
                }
                self.q.put_nowait(json.dumps(tool_use).encode())

    async def tool_opener(model_id, region):
        s = _ToolStream()
        streams.append(s)
        return s

    monkeypatch.setattr(voice_session, "open_stream", tool_opener)

    c = _client()
    with c.websocket_connect("/ws/voice?session_id=abc") as ws:
        ws.send_json({"type": "start", "history": [], "model_key": "opus5.0"})
        assert ws.receive_json()["type"] == "ready"
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "text", "content": "go"})
        seen = []
        for _ in range(8):
            ev = ws.receive_json()
            seen.append(ev["type"])
            if ev["type"] == "tool_result":
                break
        assert "tool_result" in seen
        ws.send_json({"type": "end"})
        draining = ws.receive_json()
        while draining["type"] != "draining":  # state changes may precede it
            draining = ws.receive_json()
        assert draining["runs"][0]["run_id"] == "r1"
        ws.send_json({"type": "cancel"})
        ended = ws.receive_json()
        assert ended == {"type": "ended", "reason": "user"}
    assert cancelled["hit"]
