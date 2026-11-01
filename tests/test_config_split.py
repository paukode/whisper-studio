"""The SYSTEM/USER config split (server/infrastructure/config.py).

Covers the fresh-install seed, the old config.json that is no longer read, the
additive SYSTEM+USER catalog merge
through the live layers, and the headline goal: a user's added model survives an
app update, and a shipped model's field change flows through to a user who never
overrode it.
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

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
    monkeypatch.setenv("WHISPER_USER_DIR", str(tmp_path))  # config_dir() -> tmp (seed)
    monkeypatch.setattr(cfg, "EXAMPLE_CONFIG_PATH", str(example))
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", str(tmp_path / "config.user.json"))
    monkeypatch.setattr(cfg, "LEGACY_CONFIG_PATH", str(tmp_path / "config.json"))
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


# ── First-run seed and the old config.json ───────────────────────────────


def test_fresh_install_seeds_first_run_user_config(split_env):
    tmp = split_env
    assert cfg.seed_user_config() is True
    assert json.loads((tmp / "config.user.json").read_text()) == cfg.FIRST_RUN_USER_CONFIG


def test_seed_never_overwrites_an_existing_user_config(split_env):
    tmp = split_env
    (tmp / "config.user.json").write_text(json.dumps({"mine": True}))
    assert cfg.seed_user_config() is False
    assert json.loads((tmp / "config.user.json").read_text()) == {"mine": True}


def test_old_config_json_is_never_read_moved_or_seeded_over(split_env, monkeypatch):
    tmp = split_env
    old_text = json.dumps({"mine": True, "model_mode": "local"}, indent=4)
    (tmp / "config.json").write_text(old_text)

    # No first-run seed while config.json exists: the shipped defaults apply.
    assert cfg.seed_user_config() is False
    assert not (tmp / "config.user.json").exists()
    _reset(monkeypatch)
    c = cfg.load_config()
    assert "mine" not in c
    assert c["model_mode"] == cfg.DEFAULTS["model_mode"]

    # Saving a setting creates config.user.json; config.json stays as it was.
    cfg.save_config({"effort_level": "low"})
    assert json.loads((tmp / "config.user.json").read_text()) == {"effort_level": "low"}
    assert (tmp / "config.json").read_text() == old_text
    assert glob.glob(str(tmp / "config.json.*")) == []


def test_old_config_json_gets_a_notice_until_it_is_gone(split_env):
    tmp = split_env
    assert cfg.old_config_notices() == []

    (tmp / "config.json").write_text("{}")
    [(title, message)] = cfg.old_config_notices()
    assert title == "config.json is no longer read"
    assert str(tmp / "config.json") in message
    assert "does not exist yet" in message

    (tmp / "config.user.json").write_text("{}")
    [(_, message)] = cfg.old_config_notices()
    assert "does not exist yet" not in message

    (tmp / "config.json").unlink()
    assert cfg.old_config_notices() == []


def test_local_mode_is_not_followed_and_gets_a_notice(split_env, monkeypatch):
    tmp = split_env
    (tmp / "config.user.json").write_text(json.dumps({"local_mode": True}))
    _reset(monkeypatch)
    assert cfg.load_config()["model_mode"] == cfg.DEFAULTS["model_mode"]
    [(title, _)] = cfg.old_config_notices()
    assert title == "local_mode is no longer read"

    # With an explicit model_mode nothing changed for this user: no notice.
    (tmp / "config.user.json").write_text(json.dumps({"local_mode": True, "model_mode": "local"}))
    assert cfg.old_config_notices() == []


def test_startup_reports_old_config_without_touching_it(monkeypatch):
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    import server.main as main_mod
    import server.notifications as notifications

    legacy = Path(cfg.LEGACY_CONFIG_PATH)  # the conftest gives each test its own
    legacy.write_text("{}")
    seen = []
    monkeypatch.setattr(notifications, "record_notification", lambda **kw: seen.append(kw))

    main_mod._report_old_config()

    assert [(n["source"], n["status"]) for n in seen] == [("config", "warning")]
    assert legacy.read_text() == "{}"


def test_startup_config_steps_never_create_the_user_config(monkeypatch):
    """The lifespan steps that touch the user layer (small-model sweep, retired
    keys, old-config notice) leave a home with only config.json as it was."""
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    import server.main as main_mod
    import server.notifications as notifications
    from server.model_browser.install import disable_tools_on_small_models

    legacy = Path(cfg.LEGACY_CONFIG_PATH)
    legacy.write_text('{"model_mode": "local"}')
    monkeypatch.setattr(notifications, "record_notification", lambda **kw: None)

    disable_tools_on_small_models()
    main_mod._retire_config_keys()
    main_mod._report_old_config()

    assert not Path(cfg.USER_CONFIG_PATH).exists()
    assert legacy.read_text() == '{"model_mode": "local"}'


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
    """A config.user.json whose older Sonnet 5 entries lack an explicit
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
    (tmp / "config.user.json").write_text(json.dumps(legacy))

    meta = cfg.load_config()["chat_model_meta"]
    # What startup resolves for a Sonnet 5 entry that lacks the explicit field.
    expected = infer_supports_ultracode({"id": sonnet_id}, "sonnet5")
    assert expected is True
    assert meta["sonnet5"]["supports_ultracode"] is expected
    assert meta["flat5"]["supports_ultracode"] is expected
    # Explicit false wins over inference.
    assert meta["muted5"]["supports_ultracode"] is False
