"""/api/models names who runs each model, so the composer can group its menu.

The rule matches how a turn is routed: an on-device model is local, a model
configured with the openai_bedrock provider is OpenAI, every other cloud
model is Claude on Bedrock.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_models_endpoint_reports_the_provider(monkeypatch):
    import server.chat.infra as infra
    from server.chat import router as chat_router

    meta = {
        "opus": {"label": "Opus"},
        "gpt": {"label": "GPT", "provider": "openai_bedrock"},
        "gemma": {"label": "Gemma", "is_local": True},
    }
    monkeypatch.setattr(
        infra, "mode_chat_catalog", lambda _cfg: (["opus", "gpt", "gemma"], meta, "cloud", "opus")
    )
    app = FastAPI()
    app.include_router(chat_router)
    rows = {m["key"]: m["provider"] for m in TestClient(app).get("/api/models").json()["models"]}
    assert rows == {"opus": "anthropic", "gpt": "openai", "gemma": "local"}
