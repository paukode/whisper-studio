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


def test_user_chat_models_merge_additively_over_system(monkeypatch):
    # After the config split, the SYSTEM catalog (config.example.json) always
    # appears; a USER entry ADDS a new key or overrides individual fields of a
    # shipped one via a per-key/per-field deep-merge — it no longer REPLACES the
    # catalog wholesale. This is the headline behavior change.
    from server.infrastructure import config as cfg

    monkeypatch.setattr(
        cfg,
        "_load_user_config",
        lambda: {
            "chat_models": {
                # A brand-new user model (not shipped).
                "my_local": {
                    "id": "local:my-local",
                    "label": "My Local",
                    "is_local": True,
                    "repo_id": "acme/my-local",
                    "filename": "m.gguf",
                    "dir": "my-local",
                    "ctx": 8192,
                },
                # A per-field override of a shipped model: change only the label,
                # leaving the shipped id/thinking intact.
                "haiku": {"label": "My Haiku"},
            },
        },
    )
    cfg._invalidate_cache()
    try:
        c = cfg.load_config()
        ids, meta = c["chat_models"], c["chat_model_meta"]
        # Shipped models are still present …
        assert "opus4.8" in ids and "sonnet" in ids and "haiku" in ids
        # … the user's new model was added …
        assert ids["my_local"] == "local:my-local"
        # … and the per-field override applied without dropping the shipped id.
        assert meta["haiku"]["label"] == "My Haiku"
        assert ids["haiku"].startswith("global.anthropic.claude-haiku")
    finally:
        cfg._invalidate_cache()


def test_chat_models_disabled_hides_a_system_model(monkeypatch):
    # A key listed in chat_models_disabled is dropped from the effective catalog
    # after the merge, letting a user hide a shipped model they don't want.
    from server.infrastructure import config as cfg

    monkeypatch.setattr(
        cfg,
        "_load_user_config",
        lambda: {"chat_models_disabled": ["haiku", "sonnet"]},
    )
    cfg._invalidate_cache()
    try:
        ids = cfg.load_config()["chat_models"]
        assert "haiku" not in ids and "sonnet" not in ids
        # Non-hidden shipped models remain.
        assert "opus4.8" in ids
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
