"""The Origin/Host guard must reject cross-site requests (CSRF / DNS-rebinding)
while leaving same-origin and header-less (curl) requests alone."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure.security import origin_guard


def _client() -> TestClient:
    app = FastAPI()
    app.middleware("http")(origin_guard)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.post("/do")
    def do():
        return {"done": True}

    # base_url=localhost so the TestClient's default Host header is trusted.
    return TestClient(app, base_url="http://localhost")


def test_request_without_origin_is_allowed():
    c = _client()
    assert c.get("/ping").status_code == 200
    assert c.post("/do").status_code == 200


def test_same_origin_localhost_allowed():
    c = _client()
    assert c.post("/do", headers={"Origin": "http://localhost:8001"}).status_code == 200
    assert c.post("/do", headers={"Origin": "http://127.0.0.1:9000"}).status_code == 200


def test_cross_site_origin_rejected():
    c = _client()
    r = c.post("/do", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_untrusted_host_header_rejected():
    c = _client()
    assert c.get("/ping", headers={"Host": "evil.example"}).status_code == 403


def test_trusted_origin_via_env(monkeypatch):
    monkeypatch.setenv("WHISPER_TRUSTED_ORIGINS", "my-box")
    c = _client()
    assert c.post("/do", headers={"Origin": "http://my-box:9000"}).status_code == 200
