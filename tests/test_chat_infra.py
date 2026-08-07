"""The chat catalog helpers (server/chat/infra.py).

Focus: a downloaded on-device model with NO config entry (a recommended model the
user downloaded, or one left on disk by a prior release) must still be selectable
and routable. The app ships no local chat models, so the registry — not config —
is the source of truth for what local models exist; these helpers fold the
registry's downloaded locals into the chat catalog the picker and router read.
"""

from __future__ import annotations

from server.chat import infra


def _patch(monkeypatch, config: dict, registry: dict) -> None:
    monkeypatch.setattr(infra, "load_config", lambda *a, **k: config)
    monkeypatch.setattr("server.local.registry.local_models", lambda: registry)


def test_downloaded_local_without_config_entry_is_folded_into_catalog(monkeypatch):
    config = {
        "chat_models": {"opus5.0": "global.anthropic.claude-opus-5"},
        "chat_model_meta": {"opus5.0": {"label": "Opus 5", "is_local": False}},
    }
    registry = {
        "local_gemma": {
            "id": "local:gemma-4-12b-it-qat-q4_0",
            "label": "Gemma 4 12B (Local)",
            "is_local": True,
            "supports_thinking": True,
            "supports_tools": True,
        }
    }
    _patch(monkeypatch, config, registry)

    models = infra._get_chat_models()
    assert models["opus5.0"] == "global.anthropic.claude-opus-5"  # config kept
    assert models["local_gemma"] == "local:gemma-4-12b-it-qat-q4_0"  # orphan folded in

    meta = infra._get_chat_model_meta()
    assert meta["local_gemma"]["is_local"] is True
    assert meta["local_gemma"]["label"] == "Gemma 4 12B (Local)"
    assert meta["local_gemma"]["supports_tools"] is True


def test_config_local_entry_is_not_overwritten_by_registry(monkeypatch):
    """A model the user configured wins — the registry never clobbers its id/meta
    (setdefault only fills genuinely missing keys)."""
    config = {
        "chat_models": {"local_gemma": "local:user-override"},
        "chat_model_meta": {"local_gemma": {"label": "My Gemma", "is_local": True}},
    }
    registry = {"local_gemma": {"id": "local:gemma-4-12b-it-qat-q4_0", "label": "Shipped"}}
    _patch(monkeypatch, config, registry)

    assert infra._get_chat_models()["local_gemma"] == "local:user-override"
    assert infra._get_chat_model_meta()["local_gemma"]["label"] == "My Gemma"


def test_no_local_models_leaves_the_catalog_as_config(monkeypatch):
    """Fresh install: nothing downloaded, so the catalog is exactly the config's
    cloud models — no local rows appear."""
    config = {
        "chat_models": {"opus5.0": "global.anthropic.claude-opus-5"},
        "chat_model_meta": {"opus5.0": {"label": "Opus 5"}},
    }
    _patch(monkeypatch, config, {})
    assert infra._get_chat_models() == {"opus5.0": "global.anthropic.claude-opus-5"}
    assert "local_gemma" not in infra._get_chat_model_meta()
