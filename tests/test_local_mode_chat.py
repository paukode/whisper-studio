"""Local mode for the chat model list and chat turns.

Local mode promises that nothing leaves this Mac. The contracts pinned here:

* /api/models offers only on-device models in local mode, and with none
  installed it offers nothing (plus an explicit ``needs_local_model``), never
  the cloud catalog as a stand-in.
* A chat turn in local mode that names a cloud model is refused with the
  existing error frame before any Bedrock client or adapter exists.
* A model the live catalog gained after the session latched (a Discover
  install mid-session, or weights on disk with no config entry) runs in that
  session instead of being swapped for the latched cloud default.
* Side paths that would reach Bedrock (/btw, /subagent) stay local.

The user layer and the on-device registry are patched where production reads
them (``config._load_user_config`` and ``server.local.registry.local_models``),
so the real load_config, latch and routing run on top.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from server.chat import routes, stream_slot
from server.infrastructure import config as cfg
from server.local.registry import RECOMMENDED_LOCAL_MODELS

GEMMA = {**RECOMMENDED_LOCAL_MODELS["local_gemma"], "is_local": True}


@pytest.fixture
def user_layer(monkeypatch):
    """The user config layer as a mutable dict (call ``cfg._invalidate_cache()``
    after changing it mid-test)."""
    layer: dict = {}
    monkeypatch.setattr(cfg, "_load_user_config", lambda: json.loads(json.dumps(layer)))
    cfg._invalidate_cache()
    yield layer
    cfg._invalidate_cache()


@pytest.fixture
def registry(monkeypatch):
    """The on-device registry (downloaded models) as a mutable dict."""
    models: dict = {}
    monkeypatch.setattr("server.local.registry.local_models", lambda: dict(models))
    return models


@pytest.fixture
def no_cloud(monkeypatch):
    """Any Bedrock client or cloud adapter construction fails the test."""
    import server.chat.engine.anthropic as engine_anthropic
    import server.chat.engine.openai as engine_openai

    def _boom(*_a, **_k):
        raise AssertionError("cloud path reached")

    monkeypatch.setattr(routes, "_get_bedrock_client", _boom)
    monkeypatch.setattr(engine_anthropic, "AnthropicAdapter", _boom)
    monkeypatch.setattr(engine_openai, "OpenAIResponsesAdapter", _boom)


@pytest.fixture
def local_path(monkeypatch):
    """Stub the on-device bridge where routes resolves it; records the key."""
    seen: list[str] = []

    def _stub(**kw):
        from server.local.runtime import is_local_model

        if not is_local_model(kw["model_key"]):
            return None
        seen.append(kw["model_key"])

        async def _frames():
            yield 'data: {"text": "local reply"}\n\n'
            yield "data: [DONE]\n\n"

        return StreamingResponse(_frames(), media_type="text/event-stream")

    monkeypatch.setattr("server.local.route.local_chat_response", _stub)
    return seen


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router)
    sessions: list[str] = []
    c = TestClient(app)
    c.sessions = sessions  # type: ignore[attr-defined]
    yield c
    for sid in sessions:
        cfg.unlatch_session(sid)
    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()


def _turn(client, session_id: str, **body) -> list[dict]:
    """POST /api/chat and return the decoded data frames (without [DONE])."""
    client.sessions.append(session_id)
    # Each request is its own turn here, isolated from the one before; the
    # slot's release has its own tests in test_chat_slot_release.py.
    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()
    payload = {"question": "hey", "history": [], "session_id": session_id, **body}
    with client.stream("POST", "/api/chat", json=payload) as resp:
        raw = b"".join(resp.iter_raw()).decode()
    frames = []
    for line in raw.splitlines():
        if line.startswith("data: ") and line[6:] != "[DONE]":
            frames.append(json.loads(line[6:]))
    return frames


def _errors(frames: list[dict]) -> list[dict]:
    return [f for f in frames if "error" in f]


# ── /api/models ──────────────────────────────────────────────────────────────


def test_local_mode_with_no_on_device_model_lists_nothing(user_layer, registry, client):
    user_layer["model_mode"] = "local"
    data = client.get("/api/models").json()
    assert data["models"] == []
    assert data["default"] == ""
    assert data["needs_local_model"] is True


def test_local_mode_lists_only_on_device_models(user_layer, registry, client):
    user_layer["model_mode"] = "local"
    registry["local_gemma"] = GEMMA
    data = client.get("/api/models").json()
    keys = [m["key"] for m in data["models"]]
    assert "local_gemma" in keys
    assert all(m["is_local"] for m in data["models"])
    assert data["default"] in keys
    assert data["needs_local_model"] is False


def test_cloud_mode_default_is_an_offered_model(user_layer, registry, client):
    user_layer["model_mode"] = "cloud"
    registry["local_gemma"] = GEMMA
    data = client.get("/api/models").json()
    keys = [m["key"] for m in data["models"]]
    assert keys and not any(m["is_local"] for m in data["models"])
    assert data["default"] in keys
    assert data["needs_local_model"] is False


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A connected project; returns a writer for its .whisper/settings.json.
    Patched where routes resolves the workspace (its module-level import)."""
    monkeypatch.setattr(routes, "get_workspace_path", lambda: str(tmp_path))

    def _write(settings: dict) -> None:
        (tmp_path / ".whisper").mkdir(exist_ok=True)
        (tmp_path / ".whisper" / "settings.json").write_text(json.dumps(settings))

    return _write


def test_a_model_the_project_hides_is_not_offered_and_never_the_default(
    user_layer, registry, workspace, client
):
    user_layer["model_mode"] = "cloud"
    hidden = cfg.load_config()["default_chat_model"]
    assert hidden in [m["key"] for m in client.get("/api/models").json()["models"]]

    workspace({"chat_models_disabled": [hidden]})
    data = client.get("/api/models").json()
    keys = [m["key"] for m in data["models"]]
    assert keys and hidden not in keys
    assert data["default"] in keys


def test_a_downloaded_model_stays_offered_when_the_project_replaces_the_catalog(
    user_layer, registry, workspace, client
):
    """The on-device model has a config entry globally, but the project's own
    catalog (which replaces the global one) does not list it. It is on disk, so
    the picker compares the registry against the project's catalog and still
    offers it, the same way the turn's catalog does."""
    user_layer["model_mode"] = "local"
    user_layer["chat_models"] = {"local_gemma": GEMMA}
    registry["local_gemma"] = GEMMA
    cloud_key = cfg.load_config()["default_chat_model"]
    cloud_id = cfg.load_config()["chat_models"][cloud_key]

    workspace({"chat_models": {cloud_key: cloud_id}})
    data = client.get("/api/models").json()
    assert [m["key"] for m in data["models"]] == ["local_gemma"]
    assert data["default"] == "local_gemma"


# ── /api/chat ────────────────────────────────────────────────────────────────


def test_local_mode_refuses_a_cloud_model_before_any_cloud_call(
    user_layer, registry, no_cloud, local_path, client
):
    user_layer["model_mode"] = "local"
    cloud_key = cfg.load_config()["default_chat_model"]
    errors = _errors(_turn(client, "lm-refuse", model=cloud_key))
    assert len(errors) == 1
    assert errors[0]["error_code"] == "LOCAL_MODE_CLOUD_MODEL"
    assert "Local mode" in errors[0]["error"]
    assert local_path == []


def test_local_mode_with_no_model_named_never_uses_the_cloud_default(
    user_layer, registry, no_cloud, local_path, client
):
    user_layer["model_mode"] = "local"
    errors = _errors(_turn(client, "lm-nomodel"))
    assert [e["error_code"] for e in errors] == ["NO_CHAT_MODEL"]
    # With an on-device model installed, the mode's default is that model.
    registry["local_gemma"] = GEMMA
    assert _errors(_turn(client, "lm-nomodel-2")) == []
    assert local_path == ["local_gemma"]


def test_model_installed_mid_session_runs_in_that_session(
    user_layer, registry, no_cloud, local_path, client
):
    user_layer["model_mode"] = "local"
    cloud_key = cfg.load_config()["default_chat_model"]
    # Turn 1 latches the session while the catalog is still cloud-only.
    assert _errors(_turn(client, "lm-latch", model=cloud_key))
    # The user installs Gemma from Discover and picks it in the SAME session.
    registry["local_gemma"] = GEMMA
    frames = _turn(client, "lm-latch", model="local_gemma")
    assert _errors(frames) == []
    assert local_path == ["local_gemma"]
    assert {"text": "local reply"} in frames


def test_on_device_model_with_no_config_entry_runs_in_a_new_session(
    user_layer, registry, no_cloud, local_path, client
):
    user_layer["model_mode"] = "local"
    registry["local_gemma"] = GEMMA
    assert "local_gemma" not in cfg.load_config()["chat_models"]
    assert _errors(_turn(client, "lm-registry-only", model="local_gemma")) == []
    assert local_path == ["local_gemma"]


def test_unknown_model_is_refused_not_swapped_for_the_default(
    user_layer, registry, no_cloud, local_path, client
):
    user_layer["model_mode"] = "cloud"
    errors = _errors(_turn(client, "lm-unknown", model="no-such-model"))
    assert [e["error_code"] for e in errors] == ["UNKNOWN_MODEL"]
    assert "no-such-model" in errors[0]["error"]


def test_hybrid_mode_lets_a_cloud_model_through_the_gate(
    user_layer, registry, no_cloud, local_path, client
):
    user_layer["model_mode"] = "hybrid"
    cloud_key = cfg.load_config()["default_chat_model"]
    errors = _errors(_turn(client, "lm-hybrid", model=cloud_key))
    # The turn got past model resolution to the (stubbed) cloud adapter.
    assert errors and "cloud path reached" in errors[0]["error"]
    # Not refused by the mode gate: the stub failed the setup past it.
    assert errors[0].get("error_code") == "TURN_SETUP_FAILED"


# ── side paths ───────────────────────────────────────────────────────────────


def test_local_mode_btw_is_refused_without_calling_bedrock(user_layer, no_cloud, client):
    user_layer["model_mode"] = "local"
    with client.stream("POST", "/api/chat/btw", json={"question": "what time is it"}) as r:
        raw = b"".join(r.iter_raw()).decode()
    frames = [json.loads(ln[6:]) for ln in raw.splitlines() if ln.startswith("data: {")]
    assert frames and "Local mode" in frames[0]["error"]


def test_local_mode_subagent_refuses_a_cloud_model(user_layer, registry, monkeypatch, client):
    user_layer["model_mode"] = "local"

    async def _never(*_a, **_k):
        raise AssertionError("agent started")

    monkeypatch.setattr("server.agents.runtime.run_agent", _never)
    cloud_key = cfg.load_config()["default_chat_model"]
    body = {"task": "list files", "model": cloud_key, "session_id": "lm-sub"}
    with client.stream("POST", "/api/subagent/stream", json=body) as r:
        raw = b"".join(r.iter_raw()).decode()
    frames = [json.loads(ln[6:]) for ln in raw.splitlines() if ln.startswith("data: {")]
    assert frames and "Local mode" in frames[0]["error"]
