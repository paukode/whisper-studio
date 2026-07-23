"""Model-mode + per-capability backend resolution and chat-model visibility."""

from server.infrastructure import model_mode as mm


def test_current_mode_default_and_coercion():
    assert mm.current_mode({}) == "cloud"
    assert mm.current_mode({"model_mode": "local"}) == "local"
    assert mm.current_mode({"model_mode": "hybrid"}) == "hybrid"
    # Unknown / blank coerces to the default.
    assert mm.current_mode({"model_mode": "bogus"}) == "cloud"
    assert mm.current_mode({"model_mode": ""}) == "cloud"


def test_resolve_backend_cloud():
    cfg = {"model_mode": "cloud"}
    assert mm.resolve_backend("embed", cfg) == "cohere"
    assert mm.resolve_backend("rerank", cfg) == "cohere"
    assert mm.resolve_backend("ner", cfg) == "haiku"
    assert mm.resolve_backend("index_llm", cfg) == "haiku"


def test_resolve_backend_local():
    cfg = {"model_mode": "local"}
    assert mm.resolve_backend("embed", cfg) == "qwen3"
    assert mm.resolve_backend("rerank", cfg) == "qwen3"
    assert mm.resolve_backend("ner", cfg) == "gliner"
    assert mm.resolve_backend("index_llm", cfg) == "local"


def test_resolve_backend_hybrid_uses_overrides_then_cloud_default():
    cfg = {"model_mode": "hybrid", "backends": {"embed": "qwen3", "ner": "gliner"}}
    assert mm.resolve_backend("embed", cfg) == "qwen3"  # override
    assert mm.resolve_backend("ner", cfg) == "gliner"  # override
    assert mm.resolve_backend("rerank", cfg) == "cohere"  # unset -> cloud default
    assert mm.resolve_backend("index_llm", cfg) == "haiku"  # unset -> cloud default


def test_resolve_backend_rejects_unknown_capability():
    import pytest

    with pytest.raises(ValueError):
        mm.resolve_backend("speech", {"model_mode": "cloud"})


def test_visibility_cloud_hides_local_models():
    meta = {
        "opus4.8": {},
        "gpt5.5": {"provider": "openai_bedrock"},
        "local_gemma": {"is_local": True},
    }
    keys = ["opus4.8", "gpt5.5", "local_gemma"]
    assert mm.visible_chat_keys(keys, meta, "cloud") == ["opus4.8", "gpt5.5"]


def test_visibility_local_shows_only_local_models():
    meta = {"opus4.8": {}, "local_gemma": {"is_local": True}, "local_coder": {"is_local": True}}
    keys = ["opus4.8", "local_gemma", "local_coder"]
    assert mm.visible_chat_keys(keys, meta, "local") == ["local_gemma", "local_coder"]


def test_visibility_hybrid_shows_all():
    meta = {"opus4.8": {}, "local_gemma": {"is_local": True}}
    keys = ["opus4.8", "local_gemma"]
    assert mm.visible_chat_keys(keys, meta, "hybrid") == keys


def test_visibility_empty_filter_falls_back_to_all():
    # A cloud install left in local mode with no on-device models must not
    # produce a blank picker.
    meta = {"opus4.8": {}, "gpt5.5": {}}
    keys = ["opus4.8", "gpt5.5"]
    assert mm.visible_chat_keys(keys, meta, "local") == keys


def test_config_defaults_expose_model_mode():
    from server.infrastructure.config import DEFAULTS

    assert DEFAULTS["model_mode"] == "cloud"
    assert DEFAULTS["backends"] == {}


def test_model_mode_inferred_from_local_mode_when_unset(monkeypatch):
    # A pre-existing on-device config (local_mode on, no model_mode) follows
    # local_mode so its on-device models aren't hidden by the cloud default.
    from server.infrastructure import config as cfg

    monkeypatch.setattr(cfg, "_load_user_config", lambda: {"local_mode": True})
    cfg._invalidate_cache()
    try:
        assert cfg.load_config().get("model_mode") == "local"
    finally:
        cfg._invalidate_cache()


def test_explicit_model_mode_wins_over_local_mode(monkeypatch):
    from server.infrastructure import config as cfg

    monkeypatch.setattr(
        cfg, "_load_user_config", lambda: {"local_mode": True, "model_mode": "cloud"}
    )
    cfg._invalidate_cache()
    try:
        assert cfg.load_config().get("model_mode") == "cloud"
    finally:
        cfg._invalidate_cache()


def test_fresh_config_defaults_to_cloud(monkeypatch):
    from server.infrastructure import config as cfg

    monkeypatch.setattr(cfg, "_load_user_config", lambda: {})
    cfg._invalidate_cache()
    try:
        assert cfg.load_config().get("model_mode") == "cloud"
    finally:
        cfg._invalidate_cache()


def test_config_chat_models_replace_defaults_not_union(monkeypatch):
    # config.json is the single source of truth for the model catalog: it
    # REPLACES the built-in DEFAULTS rather than unioning under them, so a key
    # the config doesn't list (e.g. DEFAULTS' "opus4.6") is NOT injected — which
    # is what used to surface a second "Opus 4.6" alongside a config "opus".
    from server.infrastructure import config as cfg

    monkeypatch.setattr(
        cfg,
        "_load_user_config",
        lambda: {
            "chat_models": {
                "opus": {
                    "id": "global.anthropic.claude-opus-4-6-v1",
                    "label": "Opus 4.6",
                    "thinking": "budget",
                },
            },
            "default_chat_model": "opus",
        },
    )
    cfg._invalidate_cache()
    try:
        c = cfg.load_config()
        ids, meta = c["chat_models"], c["chat_model_meta"]
        # Only the config's model exists — DEFAULTS (haiku/sonnet/opus4.6/...) are
        # not merged in.
        assert list(ids.keys()) == ["opus"]
        assert "opus4.6" not in ids and "haiku" not in ids
        assert [k for k, m in meta.items() if m.get("label") == "Opus 4.6"] == ["opus"]
        assert c["default_chat_model"] == "opus"
    finally:
        cfg._invalidate_cache()


def test_default_catalog_is_sourced_from_the_template_not_a_code_copy():
    # Single source of truth: the code keeps no separate catalog; DEFAULTS loads
    # it from config.example.json, so the two can't drift (which is what caused
    # the us.* vs global.* mismatch).
    import json

    from server.infrastructure.config import DEFAULTS, EXAMPLE_CONFIG_PATH

    with open(EXAMPLE_CONFIG_PATH) as f:
        template = json.load(f).get("chat_models")
    assert DEFAULTS["chat_models"] == template
    # Opus ids resolve to the global inference profile (the way it works).
    for key, entry in DEFAULTS["chat_models"].items():
        if key.startswith("opus"):
            assert entry["id"].startswith("global."), (key, entry["id"])


def test_defaults_catalog_used_only_when_config_omits_chat_models(monkeypatch):
    # The hardcoded catalog is a fallback: a config with no chat_models still
    # gets a working model list so the picker is never empty.
    from server.infrastructure import config as cfg

    monkeypatch.setattr(cfg, "_load_user_config", lambda: {"bedrock_region": "us-east-1"})
    cfg._invalidate_cache()
    try:
        ids = cfg.load_config()["chat_models"]
        assert "opus4.6" in ids and "haiku" in ids  # DEFAULTS catalog
    finally:
        cfg._invalidate_cache()
