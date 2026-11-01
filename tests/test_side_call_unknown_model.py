"""/btw and /subagent refuse a model the catalog does not know, like /api/chat,
instead of running the request on another model."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from server.chat import routes

    monkeypatch.setattr("server.infrastructure.model_mode.current_mode", lambda *a, **k: "cloud")
    monkeypatch.setattr(routes, "_get_chat_models", lambda: {"haiku": "anthropic.haiku-x"})

    def _no_bedrock():
        raise AssertionError("an unknown model must never reach Bedrock")

    monkeypatch.setattr(routes, "_get_bedrock_client", _no_bedrock)
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def _frames(r) -> list:
    return [
        json.loads(line[len("data: ") :])
        for line in r.iter_lines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/chat/btw", {"question": "what is X", "model": "gone-model"}),
        ("/api/subagent/stream", {"task": "look into X", "model": "gone-model"}),
    ],
)
def test_an_unknown_model_is_refused_with_a_reason(client, path, body):
    with client.stream("POST", path, json=body) as r:
        frames = _frames(r)
    assert frames == [
        {
            "error": "gone-model is not an available chat model (it may have been removed). "
            "Pick one from the model menu."
        }
    ]
