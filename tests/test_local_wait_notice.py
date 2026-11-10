"""An on-device round that queues behind another request says so.

llama-server answers one request at a time (--parallel 1). With several
sessions on the same on-device model, a round sent while another one streams
waits for it with nothing on screen. The adapter now sends a status first,
and the first token clears it. mlx_lm batches requests that arrive together,
so an MLX round never waits like that.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import server.local.server_stream as SS
from server.chat.engine.events import Frame, TextDelta
from server.chat.engine.local import WAITING_FOR_MODEL, LocalAdapter
from server.chat.engine.policy import LOCAL_POLICY
from server.chat.engine.runner import TurnContext, run_turn
from server.local import serving


async def _one_answer(base_url, payload):
    yield ("text", "hello")
    yield ("done", {"calls": [], "finish_reason": "stop", "usage": {}})


def _adapter() -> LocalAdapter:
    return LocalAdapter(
        model_key="local_gemma",
        base_url="http://127.0.0.1:9",
        system_prompt="sys",
        thinking_level=None,
        tools_enabled=False,
    )


def _round_events(adapter: LocalAdapter) -> list:
    async def go():
        return [
            e
            async for e in adapter.stream_round(
                [{"role": "user", "content": "q"}], [], None, 0, False
            )
        ]

    return asyncio.run(go())


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(SS, "_stream_round", _one_answer)

    def use(name: str) -> None:
        monkeypatch.setattr(serving, "engine_of", lambda key: name)

    return use


def test_a_round_behind_another_request_says_it_waits_then_streams(engine):
    engine("gguf")

    with serving.counted_request():  # another session's round
        events = _round_events(_adapter())

    assert events[0] == Frame(payload={"status": WAITING_FOR_MODEL})
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["hello"]


def test_a_round_with_the_model_free_says_nothing(engine):
    engine("gguf")

    events = _round_events(_adapter())

    assert not [e for e in events if isinstance(e, Frame)]


def test_an_mlx_round_never_says_it_waits(engine):
    engine("mlx")

    with serving.counted_request():
        events = _round_events(_adapter())

    assert not [e for e in events if isinstance(e, Frame)]


def test_the_notice_reaches_the_chat_stream_as_a_status_frame(engine):
    engine("gguf")
    adapter = _adapter()
    ctx = TurnContext(
        cost_source="chat",
        session_id="local-wait",
        model_key="local_gemma",
        model_id="local_gemma",
        messages=[{"role": "user", "content": "hi"}],
        adapter=adapter,
        policy=LOCAL_POLICY,
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
    )

    async def go():
        return [c async for c in run_turn(ctx)]

    with serving.counted_request():
        chunks = asyncio.run(go())

    frames = [json.loads(c[len("data: ") :]) for c in chunks if c.startswith("data: {")]
    statuses = [f["status"] for f in frames if "status" in f]
    assert WAITING_FOR_MODEL in statuses
    assert any(f.get("text") == "hello" for f in frames)


def test_a_round_is_counted_while_the_server_answers_it():
    seen: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(serving.requests_in_flight())
            chunk = {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}
            body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:

        async def go():
            url = f"http://127.0.0.1:{server.server_address[1]}"
            return [p async for p in SS._stream_round(url, {"messages": []})]

        pieces = asyncio.run(go())
    finally:
        server.shutdown()

    assert ("text", "hi") in pieces
    assert seen == [1]
    assert serving.requests_in_flight() == 0


def test_a_failed_request_is_no_longer_counted():
    with pytest.raises(RuntimeError):
        with serving.counted_request():
            raise RuntimeError("the server went away")

    assert serving.requests_in_flight() == 0
