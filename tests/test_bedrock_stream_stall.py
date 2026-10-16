"""A Bedrock stream that goes silent mid-round is cut and the round re-runs.

Live failure: an Opus turn streamed thinking, then the connection died without
a reset. The socket waited out the client's 600 s read_timeout, and the turn
ended with "Read timed out." and zero retries. Bedrock pings an open stream
every 30 s, so the adapter cuts the socket timeout once the first event is in.

These tests use a real botocore client and a real socket against a local
server that speaks Bedrock's event-stream framing, because the timeout is set
on the urllib3 socket that botocore's EventStream keeps private. A botocore or
urllib3 upgrade that moves it fails here instead of silently restoring the
10-minute wait.
"""

import base64
import binascii
import json
import socketserver
import struct
import threading
import time

import botocore.session
import pytest
from botocore.config import Config

import server.chat.engine.anthropic as anthropic_mod
import server.chat.engine.runner as runner_mod
from tests.golden_harness import (
    msg_end,
    msg_start,
    run_chat_turn,
    text_block,
    thinking_block,
)

# Long enough that a test which waits it out fails the elapsed-time check.
_CLIENT_READ_TIMEOUT_S = 30


def _frame(event: dict) -> bytes:
    """One event-stream message carrying an Anthropic event as a chunk."""
    payload = json.dumps({"bytes": base64.b64encode(json.dumps(event).encode()).decode()})
    headers = b""
    for name, value in (
        (":event-type", "chunk"),
        (":content-type", "application/json"),
        (":message-type", "event"),
    ):
        headers += bytes([len(name)]) + name.encode() + b"\x07"
        headers += struct.pack(">H", len(value)) + value.encode()
    total = 12 + len(headers) + len(payload) + 4
    prelude = struct.pack(">II", total, len(headers))
    prelude += struct.pack(">I", binascii.crc32(prelude))
    message = prelude + headers + payload.encode()
    return message + struct.pack(">I", binascii.crc32(message))


class _Handler(socketserver.StreamRequestHandler):
    """Answers like Bedrock: chunked event-stream bodies on a keep-alive
    connection (with ``Connection: close`` http.client detaches the socket
    from the connection, which real Bedrock never does)."""

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")

    def handle(self):
        while self.rfile.readline():  # request line; empty when the client hangs up
            length = 0
            while (line := self.rfile.readline()) not in (b"\r\n", b"\n", b""):
                name, _, value = line.decode().partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value)
            self.rfile.read(length)
            events, stall = self.server.rounds.pop(0)
            self.server.served += 1
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/vnd.amazon.eventstream\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: keep-alive\r\n\r\n"
            )
            for event in events:
                self._chunk(_frame(event))
            self.wfile.flush()
            if stall:
                # Hold the connection open and send nothing, like a dead link.
                self.server.release.wait(_CLIENT_READ_TIMEOUT_S + 5)
                return
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()


@pytest.fixture
def bedrock_server():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.rounds = []
    server.served = 0
    server.release = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.release.set()
    server.shutdown()
    server.server_close()


def _client(port: int):
    return botocore.session.get_session().create_client(
        "bedrock-runtime",
        region_name="us-east-1",
        endpoint_url=f"http://127.0.0.1:{port}",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(read_timeout=_CLIENT_READ_TIMEOUT_S, retries={"max_attempts": 1}),
    )


def test_silent_stream_is_cut_and_the_round_reruns(monkeypatch, bedrock_server):
    monkeypatch.setattr(anthropic_mod, "_STREAM_STALL_TIMEOUT_S", 0.5)
    monkeypatch.setattr(runner_mod, "_ROUND_RETRY_BACKOFF_S", 0.0)
    bedrock_server.rounds = [
        ([msg_start(), *thinking_block("planning the next step")], True),
        ([msg_start(), *text_block("Recovered answer."), *msg_end()], False),
    ]
    client = _client(bedrock_server.server_address[1])

    started = time.monotonic()
    lines = run_chat_turn(monkeypatch, client, {"question": "go"})
    elapsed = time.monotonic() - started
    joined = "\n".join(lines)

    assert "Recovered answer." in joined
    assert '"error"' not in joined
    assert "Model stream failed; retrying..." in joined
    assert bedrock_server.served == 2
    # Cut by the stall limit, not by the client's read_timeout.
    assert elapsed < _CLIENT_READ_TIMEOUT_S / 3


def test_stall_limit_does_not_outlive_the_stream(bedrock_server):
    """The cut applies to one response. urllib3 sets the client timeout again
    for the next request, so a reused connection still waits out a long
    prefill (Bedrock holds the headers until the prompt is processed)."""
    answer = [msg_start(), *text_block("one"), *msg_end()]
    bedrock_server.rounds = [(answer, False), (answer, False)]
    client = _client(bedrock_server.server_address[1])

    def invoke():
        return client.invoke_model_with_response_stream(
            modelId="m", contentType="application/json", accept="application/json", body=b"{}"
        )["body"]

    stream = invoke()
    sock = stream._raw_stream.connection.sock
    assert sock.gettimeout() == _CLIENT_READ_TIMEOUT_S

    anthropic_mod.tighten_read_timeout(stream)
    assert sock.gettimeout() == anthropic_mod._STREAM_STALL_TIMEOUT_S
    assert len(list(stream)) == len(answer)

    next_stream = invoke()
    assert next_stream._raw_stream.connection.sock is sock  # the pooled connection
    assert sock.gettimeout() == _CLIENT_READ_TIMEOUT_S
    assert len(list(next_stream)) == len(answer)


def test_silent_side_question_stream_ends_with_an_error(monkeypatch, bedrock_server):
    """/btw has no round retry, but a dead stream still ends at the stall
    limit with an error, not after the client's read_timeout."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server.chat.routes as routes_mod

    monkeypatch.setattr(anthropic_mod, "_STREAM_STALL_TIMEOUT_S", 0.5)
    monkeypatch.setattr(routes_mod, "btw_refusal", lambda: None)
    client = _client(bedrock_server.server_address[1])
    monkeypatch.setattr(routes_mod, "_get_bedrock_client", lambda: client)
    bedrock_server.rounds = [([msg_start(), *text_block("partial")], True)]
    app = FastAPI()
    app.include_router(routes_mod.router)

    started = time.monotonic()
    with TestClient(app).stream("POST", "/api/chat/btw", json={"question": "q"}) as resp:
        raw = b"".join(resp.iter_raw()).decode()
    elapsed = time.monotonic() - started

    assert "partial" in raw
    assert "Read timed out" in raw
    assert raw.rstrip().endswith("data: [DONE]")
    assert elapsed < _CLIENT_READ_TIMEOUT_S / 3
