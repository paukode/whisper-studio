"""Regression guard: toggling a feature flag must not flatten chat_models.

The feature-flag PUT used to save load_config()'s NORMALIZED output (chat_models
flattened to id strings), which silently dropped rich per-model fields like
requires_data_retention — breaking the Fable 5 consent gate every time any flag
was toggled (e.g. switching the buddy on). It must edit the RAW on-disk config.
"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.infrastructure.config as config_mod
from server.infrastructure.feature_flags import router as ff_router

RICH_CONFIG = {
    "chat_models": {
        "opus": {"id": "global.anthropic.claude-opus-4-8", "label": "Opus", "thinking": "adaptive"},
        "fable5.0": {
            "id": "global.anthropic.claude-fable-5",
            "label": "Fable 5.0",
            "thinking": "adaptive",
            "requires_data_retention": True,
        },
    },
    "feature_flags": {"companion": False},
}


def test_toggling_flag_preserves_rich_chat_models(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(RICH_CONFIG))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg))
    config_mod._invalidate_cache()

    app = FastAPI()
    app.include_router(ff_router)
    resp = TestClient(app).put("/api/feature-flags/companion", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True

    on_disk = json.loads(cfg.read_text())
    # Flag was flipped …
    assert on_disk["feature_flags"]["companion"] is True
    # … and chat_models kept its RICH shape (dict, not a flat id string),
    # with requires_data_retention intact.
    fable = on_disk["chat_models"]["fable5.0"]
    assert isinstance(fable, dict), "chat_models was flattened — the bug"
    assert fable["requires_data_retention"] is True
    config_mod._invalidate_cache()


# A config.json with deliberate, non-json.dump formatting: aligned one-line
# model entries, specific indentation. A toggle must not disturb any of it.
HAND_FORMATTED = (
    "{\n"
    '  "tavily_api_key": "tvly-secret",\n'
    '  "chat_models": {\n'
    '    "opus":     { "id": "x", "label": "Opus", "thinking": "adaptive" },\n'
    '    "fable5.0": { "id": "y", "label": "Fable 5.0", "thinking": "adaptive", "requires_data_retention": true }\n'
    "  },\n"
    '  "feature_flags": {\n'
    '    "auto_memory": true,\n'
    '    "companion": false\n'
    "  }\n"
    "}\n"
)


def test_toggle_changes_only_the_one_boolean(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(HAND_FORMATTED)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg))
    config_mod._invalidate_cache()

    config_mod.set_feature_flag("companion", True)

    out = cfg.read_text()
    # Byte-for-byte identical except the single flipped token.
    assert out == HAND_FORMATTED.replace('"companion": false', '"companion": true')
    config_mod._invalidate_cache()


def test_toggle_other_flag_leaves_companion_and_formatting(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(HAND_FORMATTED)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg))
    config_mod._invalidate_cache()

    config_mod.set_feature_flag("auto_memory", False)

    out = cfg.read_text()
    assert out == HAND_FORMATTED.replace('"auto_memory": true', '"auto_memory": false')
    config_mod._invalidate_cache()


def test_toggle_inserts_absent_flag_without_reflowing_rest(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(HAND_FORMATTED)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg))
    config_mod._invalidate_cache()

    config_mod.set_feature_flag("dream_consolidation", True)

    data = json.loads(cfg.read_text())
    assert data["feature_flags"]["dream_consolidation"] is True
    # Pre-existing flags and the rich chat_models are untouched.
    assert data["feature_flags"]["companion"] is False
    assert isinstance(data["chat_models"]["fable5.0"], dict)
    assert data["chat_models"]["fable5.0"]["requires_data_retention"] is True
    config_mod._invalidate_cache()
