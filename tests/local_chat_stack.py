"""A local-mode chat session on the real route, with only llama-server stubbed.

The on-device path runs the real local bridge (server/local/route.py), the
real LocalAdapter and the real llama-server stream parser; the HTTP server
behind them is an httpx MockTransport and the serving layer hands out its URL.
"""

import json

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.chat import routes

LOCAL_KEY = "local_gemma"


def client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


class _LlamaServer:
    """An OpenAI-compatible /v1/chat/completions endpoint behind httpx, so the
    real _stream_round parses a real SSE body. Answers each chat request with
    the next scripted reply and records what it was sent (``requests``).

    A non-streaming request is a side call on the resident model (the goal
    judge): it gets the next of ``judge_replies`` (the last repeats) as one
    JSON completion and is recorded in ``judge_requests`` instead."""

    def __init__(self, replies, judge_replies=None):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.judge_replies = list(judge_replies or [])
        self.judge_requests: list[dict] = []

    def _judge(self, body: dict) -> httpx.Response:
        self.judge_requests.append(body)
        if not self.judge_replies:
            raise AssertionError("an unscripted judge request reached the model server")
        text = self.judge_replies[min(len(self.judge_requests), len(self.judge_replies)) - 1]
        choice = {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        return httpx.Response(200, json={"choices": [choice]})

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("stream") is False:
            return self._judge(body)
        self.requests.append(body)
        text = self.replies[min(len(self.requests), len(self.replies)) - 1]
        frames = [
            {"choices": [{"delta": {"content": text}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
        ]
        body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def install_local_stack(monkeypatch, *, replies=None, judge_replies=None, mode="local"):
    """A ``mode`` session whose model is on-device: the registry and the
    latched config carry one local model, llama-server is the stub above, and
    the serving layer hands out its URL, for turns and resident side calls
    alike (both counted in ``turns``)."""
    from server.local import registry as _reg
    from server.local import server_stream, serving

    entry = dict(_reg.RECOMMENDED_LOCAL_MODELS[LOCAL_KEY])
    monkeypatch.setattr(_reg, "_config_local_models", lambda: {LOCAL_KEY: entry})

    real_latch = routes.latch_session

    def _latch(session_id, workspace_path=None):
        cfg = real_latch(session_id, workspace_path=workspace_path)
        return {
            **cfg,
            "model_mode": mode,
            "default_chat_model": LOCAL_KEY,
            "chat_models": {**cfg.get("chat_models", {}), LOCAL_KEY: entry["id"]},
            "chat_model_meta": {
                **cfg.get("chat_model_meta", {}),
                LOCAL_KEY: {"id": entry["id"], "label": "Gemma", "is_local": True},
            },
        }

    monkeypatch.setattr(routes, "latch_session", _latch)

    server = _LlamaServer(replies or ["Hi there!", "Second answer."], judge_replies)
    real_async_client = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(server.handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory)

    turns = {"started": 0, "ended": 0}

    # Same signature as serving.ensure_serving, so a drift in the real one
    # fails here instead of being swallowed. Like the real one, only a
    # mark_busy call registers a turn.
    def _ensure_serving(key, n_ctx=None, *, should_abort=None, mark_busy=False, started=None):
        if mark_busy:
            turns["started"] += 1
        return "http://llama.test"

    def _end_turn():
        turns["ended"] += 1

    # The resident-only side call registers its own mark the same way.
    def _begin_resident_call(key):
        turns["started"] += 1
        return "http://llama.test"

    monkeypatch.setattr(serving, "ensure_serving", _ensure_serving)
    monkeypatch.setattr(serving, "end_turn", _end_turn)
    monkeypatch.setattr(serving, "begin_resident_call", _begin_resident_call)
    monkeypatch.setattr(serving, "wire_model", lambda key: "gemma")
    monkeypatch.setattr(serving, "supports_tool_choice", lambda key: True)
    monkeypatch.setattr(server_stream, "_spawn_memory_hooks", lambda *a, **kw: None)
    server.turns = turns
    return server


def post(http, sid, question):
    return http.post(
        "/api/chat",
        json={"question": question, "session_id": sid, "history": [], "model": LOCAL_KEY},
    )


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body

    async def is_disconnected(self):
        return False
