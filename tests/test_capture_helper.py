"""The capture-helper relay (server/capture_helper.py): a source install's page
reaches the helper program through one WebSocket, the same JSON lines both
ways. A stand-in helper (a shell script) replaces the Swift program."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server import capture_helper


def _stand_in(tmp_path, body: str) -> str:
    path = tmp_path / "whisper-capture"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    app = FastAPI()
    app.include_router(capture_helper.router)
    return TestClient(app, headers={"origin": "http://127.0.0.1:41430"})


def test_status_is_available_with_an_executable_helper(client, tmp_path, monkeypatch):
    monkeypatch.setattr(capture_helper, "helper_path", lambda: _stand_in(tmp_path, "exit 0"))

    assert client.get("/api/capture-helper/status").json() == {"available": True}


def test_status_is_unavailable_without_a_helper(client, tmp_path, monkeypatch):
    monkeypatch.setattr(capture_helper, "helper_path", lambda: str(tmp_path / "missing"))

    assert client.get("/api/capture-helper/status").json() == {"available": False}


def test_status_is_unavailable_in_the_packaged_app(client, tmp_path, monkeypatch):
    """The app window has the native bridges; the relay is for browsers only."""
    monkeypatch.setattr(capture_helper, "helper_path", lambda: _stand_in(tmp_path, "exit 0"))
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path))

    assert client.get("/api/capture-helper/status").json() == {"available": False}


def test_the_relay_passes_lines_both_ways(client, tmp_path, monkeypatch):
    """Each page message reaches the helper as one line, and each helper line
    reaches the page."""
    echo = 'while read -r line; do echo "{\\"channel\\":\\"audio\\",\\"event\\":\\"onEcho\\",\\"args\\":[$line]}"; done'
    monkeypatch.setattr(capture_helper, "helper_path", lambda: _stand_in(tmp_path, echo))

    with client.websocket_connect("/ws/capture-helper") as ws:
        ws.send_text(json.dumps({"channel": "audio", "cmd": "list"}))
        reply = json.loads(ws.receive_text())

    assert reply == {
        "channel": "audio",
        "event": "onEcho",
        "args": [{"channel": "audio", "cmd": "list"}],
    }


def test_a_helper_that_stops_is_reported_on_both_channels(client, tmp_path, monkeypatch):
    monkeypatch.setattr(capture_helper, "helper_path", lambda: _stand_in(tmp_path, "exit 3"))

    with client.websocket_connect("/ws/capture-helper") as ws:
        first = ws.receive_json()
        second = ws.receive_json()

    message = "The capture helper stopped (exit code 3)."
    assert first == {"channel": "audio", "event": "onError", "args": [message]}
    assert second == {"channel": "mic", "event": "onError", "args": [message]}


def test_a_missing_helper_is_an_error_not_a_silent_page(client, tmp_path, monkeypatch):
    monkeypatch.setattr(capture_helper, "helper_path", lambda: str(tmp_path / "missing"))

    with client.websocket_connect("/ws/capture-helper") as ws:
        first = ws.receive_json()

    assert first["event"] == "onError"
    assert "bash setup.sh" in first["args"][0]


def test_a_second_page_is_refused_while_one_is_connected(client, tmp_path, monkeypatch):
    """Two helpers would open the microphone twice."""
    monkeypatch.setattr(
        capture_helper, "helper_path", lambda: _stand_in(tmp_path, "cat >/dev/null")
    )

    with client.websocket_connect("/ws/capture-helper"):
        with client.websocket_connect("/ws/capture-helper") as second:
            refused = second.receive_json()
            assert second.receive_json()["channel"] == "mic"
            with pytest.raises(WebSocketDisconnect):
                second.receive_json()

    assert refused["event"] == "onError"
    assert "Another Whisper Studio page" in refused["args"][0]


def test_a_cross_site_page_is_refused(client, tmp_path, monkeypatch):
    monkeypatch.setattr(capture_helper, "helper_path", lambda: _stand_in(tmp_path, "cat"))

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/capture-helper", headers={"origin": "https://evil.example"}
        ) as ws:
            ws.receive_text()


@pytest.mark.parametrize("origin", [None, ""])
def test_a_handshake_with_no_origin_is_refused(client, tmp_path, monkeypatch, origin):
    """Only pages use the relay, and pages always send an Origin."""
    monkeypatch.setattr(capture_helper, "helper_path", lambda: _stand_in(tmp_path, "cat"))
    client.headers.pop("origin")
    headers = {} if origin is None else {"origin": origin}

    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/ws/capture-helper", headers=headers) as ws:
            ws.receive_text()
    assert refused.value.code == 1008
