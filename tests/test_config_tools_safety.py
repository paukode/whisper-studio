"""Safety guards for the config_get / config_set agent tools.

(A) execute_config_set must write the RAW on-disk config, not load_config()'s
    fully merged view. Persisting the merged view flattened chat_models to
    {key: id} (dropping label/thinking/provider/effort_tier/is_local/
    requires_data_retention — the Fable 5 consent gate), baked in the derived
    chat_model_meta key, and wrote the env TAVILY_API_KEY overlay to disk.

(B) execute_config_get must mask secrets on the explicit-`keys` branch too,
    not only the list-all branch: config_get(keys=["tavily_api_key"]) used to
    return the raw key into the tool result, model context and sessions.db.
"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.infrastructure.config as config_mod
from server.agent_tools import execute_config_get, execute_config_set

RICH_CONFIG = {
    "chat_models": {
        "opus": {
            "id": "global.anthropic.claude-opus-4-8",
            "label": "Opus",
            "thinking": "adaptive",
        },
        "fable5.0": {
            "id": "global.anthropic.claude-fable-5",
            "label": "Fable 5.0",
            "thinking": "adaptive",
            "requires_data_retention": True,
        },
    },
    "default_chat_model": "opus",
    "effort_level": "high",
}


def test_config_set_preserves_rich_shape_and_no_secret_on_disk(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(RICH_CONFIG))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg))
    # An env secret must NOT be baked into config.json by a config_set. It only
    # lives in the merged view load_config() produces; the raw-config write path
    # never sees it.
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-SECRET-1234567890")
    config_mod._invalidate_cache()

    out = json.loads(execute_config_set({"updates": {"effort_level": "medium"}}))
    assert out["updated"] == ["effort_level"]

    on_disk = json.loads(cfg.read_text())
    # The validated update landed …
    assert on_disk["effort_level"] == "medium"
    # … chat_models kept its RICH shape (dict, not a flat id string), with
    # requires_data_retention intact (the Fable 5 gate).
    fable = on_disk["chat_models"]["fable5.0"]
    assert isinstance(fable, dict), "chat_models was flattened — the bug"
    assert fable["requires_data_retention"] is True
    assert fable["id"] == "global.anthropic.claude-fable-5"
    # … the derived meta key was NOT persisted …
    assert "chat_model_meta" not in on_disk
    # … and the env secret was NOT written to disk.
    assert on_disk.get("tavily_api_key", "") == ""
    assert "tvly-SECRET-1234567890" not in cfg.read_text()
    config_mod._invalidate_cache()


def test_config_set_ignores_unknown_keys(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(RICH_CONFIG))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg))
    config_mod._invalidate_cache()

    out = json.loads(execute_config_set({"updates": {"not_a_real_key": 1}}))
    assert out["updated"] == []
    assert out["ignored"] == ["not_a_real_key"]
    assert "not_a_real_key" not in json.loads(cfg.read_text())
    config_mod._invalidate_cache()


def test_config_get_masks_tavily_on_explicit_keys(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(RICH_CONFIG))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg))
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-SECRET-1234567890")
    config_mod._invalidate_cache()

    out = json.loads(execute_config_get({"keys": ["tavily_api_key"]}))
    # The raw key must never appear …
    assert out["tavily_api_key"] != "tvly-SECRET-1234567890"
    assert "tvly-SECRET-1234567890" not in json.dumps(out)
    # … only the masked hint (first 4 chars + ***).
    assert out["tavily_api_key"] == "tvly***"
    config_mod._invalidate_cache()


def test_config_get_masks_tavily_on_list_all(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(RICH_CONFIG))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg))
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-SECRET-1234567890")
    config_mod._invalidate_cache()

    out = json.loads(execute_config_get({}))
    assert out["tavily_api_key"] == "tvly***"
    assert "tvly-SECRET-1234567890" not in json.dumps(out)
    config_mod._invalidate_cache()
