"""GET /api/config must never return the raw Tavily API key — only the masked
hint. Before the fix the handler copied the full config (including the
plaintext key) into the response and only *added* a masked field."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure.config import router as config_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(config_router)
    return TestClient(app)


def test_config_masks_and_omits_raw_tavily_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-SECRET-1234567890")
    body = _client().get("/api/config").json()

    # Raw secret must be gone…
    assert "tavily_api_key" not in body, "raw Tavily key leaked in /api/config"
    # …but the masked hint stays so the UI can show "tvly...7890".
    assert body.get("tavily_api_key_masked", "").startswith("tvly")
    # And the secret value must not appear anywhere in the response.
    assert "tvly-SECRET-1234567890" not in str(body)
