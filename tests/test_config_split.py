"""The SYSTEM/USER config split (server/infrastructure/config.py).

Covers the one-time migration of a monolithic config.json into a pruned
config.user.json, the fresh-install seed, the additive SYSTEM+USER catalog merge
through the live layers, and the headline goal: a user's added model survives an
app update, and a shipped model's field change flows through to a user who never
overrode it.
"""

from __future__ import annotations

import glob
import json

import pytest

from server.infrastructure import config as cfg

# A minimal SYSTEM catalog (config.example.json) used across the split tests.
SYSTEM_V1 = {
    "bedrock_region": "us-east-1",
    "default_chat_model": "opus5.0",
    "effort_level": "max",
    "chat_models": {
        "opus5.0": {"id": "global.anthropic.claude-opus-5", "label": "Opus 5"},
        "haiku": {"id": "global.anthropic.claude-haiku-4-5", "label": "Haiku 4.5"},
    },
}


@pytest.fixture()
def split_env(tmp_path, monkeypatch):
    """Isolate the whole config layer under a throwaway home: SYSTEM points at a
    tmp config.example.json, USER/legacy paths point into the same tmp dir, and
    the system-layer cache is cleared so each test sees its own SYSTEM catalog."""
    example = tmp_path / "config.example.json"
    example.write_text(json.dumps(SYSTEM_V1))
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path))
    monkeypatch.setenv("WHISPER_USER_DIR", str(tmp_path))  # config_dir() -> tmp (migration)
    monkeypatch.setattr(cfg, "EXAMPLE_CONFIG_PATH", str(example))
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", str(tmp_path / "config.user.json"))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    _reset(monkeypatch)
    yield tmp_path
    cfg._invalidate_cache()


def _reset(monkeypatch):
    """Drop both the system-layer cache and the merged-config cache."""
    monkeypatch.setattr(cfg, "_system_cache", None)
    monkeypatch.setattr(cfg, "_system_cache_key", None)
    cfg._invalidate_cache()


def _write_system(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# ── Migration ────────────────────────────────────────────────────────────


def test_migration_prunes_redundant_keeps_additions_and_overrides(split_env, monkeypatch):
    tmp = split_env
    monolith = {
        "bedrock_region": "eu-west-1",  # non-catalog user setting -> kept verbatim
        "tavily_api_key": "tvly-user",  # kept verbatim
        "chat_models_disabled": ["haiku"],  # kept verbatim
        "chat_models": {
            # Identical to SYSTEM -> redundant, must be DROPPED so updates flow.
            "opus5.0": {"id": "global.anthropic.claude-opus-5", "label": "Opus 5"},
            # Overrides one field of a SYSTEM entry -> real override, KEPT.
            "haiku": {"id": "global.anthropic.claude-haiku-4-5", "label": "My Haiku"},
            # Not in SYSTEM -> genuine addition, KEPT.
            "my_local": {
                "id": "local:mine",
                "is_local": True,
                "repo_id": "a",
                "filename": "f",
                "dir": "d",
            },
        },
    }
    (tmp / "config.json").write_text(json.dumps(monolith))

    assert cfg.migrate_user_config() is True

    user = json.loads((tmp / "config.user.json").read_text())
    # Non-catalog keys preserved verbatim.
    assert user["bedrock_region"] == "eu-west-1"
    assert user["tavily_api_key"] == "tvly-user"
    assert user["chat_models_disabled"] == ["haiku"]
    # Redundant copy dropped; override + addition kept.
    assert "opus5.0" not in user["chat_models"]
    assert user["chat_models"]["haiku"]["label"] == "My Haiku"
    assert user["chat_models"]["my_local"]["repo_id"] == "a"

    # A timestamped backup of the pre-split file exists …
    baks = glob.glob(str(tmp / "config.json.bak.*"))
    assert len(baks) == 1
    assert json.loads(open(baks[0]).read())["bedrock_region"] == "eu-west-1"
    # … the legacy file was renamed out of the way so it can't be double-read …
    assert not (tmp / "config.json").exists()
    assert (tmp / "config.json.pre-split").exists()


def test_migration_is_idempotent(split_env, monkeypatch):
    tmp = split_env
    (tmp / "config.json").write_text(json.dumps({"bedrock_region": "eu-west-1"}))
    assert cfg.migrate_user_config() is True
    first = (tmp / "config.user.json").read_text()

    # A second run is a no-op: it never overwrites an existing config.user.json.
    assert cfg.migrate_user_config() is False
    assert (tmp / "config.user.json").read_text() == first


def test_fresh_install_seeds_first_run_user_config(split_env):
    tmp = split_env
    # Neither config.json nor config.user.json exists.
    assert not (tmp / "config.json").exists()
    assert cfg.migrate_user_config() is False
    # Fresh install seeds the first-run defaults (hybrid + on-device index).
    assert json.loads((tmp / "config.user.json").read_text()) == cfg.FIRST_RUN_USER_CONFIG
    # No backup and no legacy rename on a clean home.
    assert glob.glob(str(tmp / "config.json.bak.*")) == []
    assert not (tmp / "config.json.pre-split").exists()


def test_migration_never_overwrites_existing_user_config(split_env):
    tmp = split_env
    (tmp / "config.user.json").write_text(json.dumps({"mine": True}))
    (tmp / "config.json").write_text(json.dumps({"bedrock_region": "eu-west-1"}))
    assert cfg.migrate_user_config() is False
    assert json.loads((tmp / "config.user.json").read_text()) == {"mine": True}
    # The legacy file is left untouched (not renamed) when already migrated.
    assert (tmp / "config.json").exists()


# ── Headline scenario: user adds a model, then the app updates ─────────────


def test_user_model_survives_app_update_and_field_changes_flow_through(split_env, monkeypatch):
    tmp = split_env
    example = tmp / "config.example.json"

    # (a) The user adds a local model to config.user.json.
    (tmp / "config.user.json").write_text(
        json.dumps(
            {
                "chat_models": {
                    "my_local": {
                        "id": "local:mine",
                        "label": "My Local",
                        "is_local": True,
                        "repo_id": "acme/mine",
                        "filename": "m.gguf",
                        "dir": "mine",
                        "ctx": 8192,
                    }
                }
            }
        )
    )
    _reset(monkeypatch)
    c = cfg.load_config()
    assert "my_local" in c["chat_models"]  # user model present
    assert "opus5.0" in c["chat_models"] and "haiku" in c["chat_models"]  # SYSTEM present

    # (b) Simulate an app update: a NEW model ships in config.example.json.
    system_v2 = json.loads(json.dumps(SYSTEM_V1))
    system_v2["chat_models"]["sonnet6"] = {
        "id": "global.anthropic.claude-sonnet-6",
        "label": "Sonnet 6",
    }
    _write_system(example, system_v2)
    _reset(monkeypatch)
    c = cfg.load_config()
    assert "sonnet6" in c["chat_models"]  # the newly-shipped model appears …
    assert c["chat_models"]["my_local"] == "local:mine"  # … and the user's model is untouched.

    # (c) A shipped model's field changes; the user never overrode it, so they see
    #     the update. (chat_models flattens ids; assert via the rich meta label.)
    system_v3 = json.loads(json.dumps(system_v2))
    system_v3["chat_models"]["haiku"]["label"] = "Haiku 4.6"
    _write_system(example, system_v3)
    _reset(monkeypatch)
    c = cfg.load_config()
    assert c["chat_model_meta"]["haiku"]["label"] == "Haiku 4.6"


# ── Registry sees a user-added model through the new layering ───────────────


def test_registry_sees_user_added_model(monkeypatch):
    """server/local/registry.local_models() resolves is_local entries from the
    merged config, so a model added only to the USER layer becomes loadable."""
    monkeypatch.setattr(
        cfg,
        "_load_user_config",
        lambda: {
            "chat_models": {
                "user_qwen": {
                    "id": "local:user-qwen",
                    "label": "User Qwen (Local)",
                    "is_local": True,
                    "supports_tools": True,
                    "repo_id": "acme/user-qwen-gguf",
                    "filename": "user-qwen-Q4_K_M.gguf",
                    "dir": "user-qwen",
                    "ctx": 16384,
                }
            }
        },
    )
    cfg._invalidate_cache()
    # No local model ships by default, so pin the on-disk check off — the user's
    # addition must stand on its own without a downloaded recommendation.
    monkeypatch.setattr(
        "server.local.registry._recommended_downloaded_on_disk", lambda entry: False
    )
    try:
        from server.local.registry import local_models

        reg = local_models()
        assert "user_qwen" in reg
        assert reg["user_qwen"]["repo_id"] == "acme/user-qwen-gguf"
        assert reg["user_qwen"]["ctx"] == 16384
        # Recommendations are NOT shipped: local_gemma appears only once downloaded.
        assert "local_gemma" not in reg
    finally:
        cfg._invalidate_cache()


# -- Legacy capability inference through the raw loader --------------------


def test_raw_load_infers_supports_ultracode_for_legacy_config(split_env):
    """A legacy config.json whose Sonnet 5 entries lack an explicit
    ``supports_ultracode`` exposes the same capability through a direct
    load_config() as startup resolves: inferred from the model id (True for
    Sonnet 5, flat-string and rich shapes alike). An explicit ``false`` is
    preserved untouched, never overridden by inference."""
    from server.infrastructure.effort import infer_supports_ultracode

    tmp = split_env
    sonnet_id = "global.anthropic.claude-sonnet-5-20250929-v1:0"
    legacy = {
        "chat_models": {
            "sonnet5": {"id": sonnet_id},  # rich shape, field absent
            "flat5": sonnet_id,  # legacy flat string, field impossible
            "muted5": {"id": sonnet_id, "supports_ultracode": False},
        }
    }
    (tmp / "config.json").write_text(json.dumps(legacy))

    meta = cfg.load_config()["chat_model_meta"]
    # What startup resolves for a Sonnet 5 entry that lacks the explicit field.
    expected = infer_supports_ultracode({"id": sonnet_id}, "sonnet5")
    assert expected is True
    assert meta["sonnet5"]["supports_ultracode"] is expected
    assert meta["flat5"]["supports_ultracode"] is expected
    # Explicit false wins over inference.
    assert meta["muted5"]["supports_ultracode"] is False
