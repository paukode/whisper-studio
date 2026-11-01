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


def test_a_home_with_only_config_json_starts_like_a_fresh_install(split_env, monkeypatch):
    tmp = split_env
    old_text = json.dumps({"mine": True, "model_mode": "cloud"}, indent=4)
    (tmp / "config.json").write_text(old_text)

    # config.json does not block the first-run seed, and nothing comes from it.
    assert cfg.seed_user_config() is True
    assert json.loads((tmp / "config.user.json").read_text()) == cfg.FIRST_RUN_USER_CONFIG
    _reset(monkeypatch)
    c = cfg.load_config()
    assert "mine" not in c
    assert c["model_mode"] == "local"

    # config.json stays exactly as it was, under its own name.
    assert (tmp / "config.json").read_text() == old_text
    assert glob.glob(str(tmp / "config.json.*")) == []


def test_old_config_json_gets_a_notice_until_it_is_gone(split_env):
    tmp = split_env
    assert cfg.old_config_notices() == []

    (tmp / "config.json").write_text("{}")
    [(title, message)] = cfg.old_config_notices()
    assert title == "config.json is no longer read"
    assert str(tmp / "config.json") in message

    # The same text once config.user.json exists, so the bell shows it once.
    (tmp / "config.user.json").write_text("{}")
    assert cfg.old_config_notices() == [(title, message)]

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


def test_retired_keys_stay_in_the_file_and_get_a_notice(split_env):
    tmp = split_env
    text = json.dumps(
        {
            "agent_limits": {"default": {"max_turns": 120}},
            "cron_max_rounds": 50,
            "brief_mode": True,
            "bedrock_region": "us-east-1",
        }
    )
    (tmp / "config.user.json").write_text(text)

    [(title, message)] = cfg.old_config_notices()

    assert title == "Old settings have no effect"
    for key in ("agent_limits", "cron_max_rounds", "brief_mode"):
        assert key in message
    assert "whisper_language" not in message
    assert (tmp / "config.user.json").read_text() == text


def test_a_retired_value_runs_as_its_replacement_in_memory_only(split_env, monkeypatch):
    tmp = split_env
    text = json.dumps({"transcription_backend": "canary"})
    (tmp / "config.user.json").write_text(text)
    _reset(monkeypatch)

    # Canary only translates now: the app records with Parakeet ("streaming").
    assert cfg.load_config()["transcription_backend"] == "streaming"
    [(title, message)] = cfg.old_config_notices()
    assert title == 'transcription_backend "canary" is no longer offered'
    assert "Parakeet" in message
    assert (tmp / "config.user.json").read_text() == text


@pytest.mark.parametrize("old", ["auto", "model"])
def test_an_old_translate_mode_runs_as_off_and_gets_a_notice(split_env, monkeypatch, old):
    tmp = split_env
    text = json.dumps({"translate_mode": old})
    (tmp / "config.user.json").write_text(text)
    _reset(monkeypatch)

    assert cfg.load_config()["translate_mode"] == "off"
    [(title, _)] = cfg.old_config_notices()
    assert title == f'translate_mode "{old}" is no longer offered'
    assert (tmp / "config.user.json").read_text() == text


def test_an_old_or_unknown_effort_level_runs_as_high_and_gets_a_notice(split_env, monkeypatch):
    tmp = split_env
    text = json.dumps({"effort_level": "auto"})
    (tmp / "config.user.json").write_text(text)
    _reset(monkeypatch)

    assert cfg.load_config()["effort_level"] == "high"
    [(title, message)] = cfg.old_config_notices()
    assert title == 'effort_level "auto" is no longer offered'
    assert "high effort is used" in message
    assert (tmp / "config.user.json").read_text() == text

    (tmp / "config.user.json").write_text(json.dumps({"effort_level": "turbo"}))
    _reset(monkeypatch)
    assert cfg.load_config()["effort_level"] == "high"
    [(title, message)] = cfg.old_config_notices()
    assert title == "Unknown setting values"
    assert 'effort_level "turbo" (high effort is used)' in message

    # A raw wire alias is known: it runs as its app label, with no notice.
    (tmp / "config.user.json").write_text(json.dumps({"effort_level": "xhigh"}))
    _reset(monkeypatch)
    assert cfg.load_config()["effort_level"] == "extra"
    assert cfg.old_config_notices() == []


def test_a_project_effort_level_auto_runs_as_high_and_gets_one_notice(split_env, monkeypatch):
    from server import notifications

    tmp = split_env
    ws = tmp / "project"
    (ws / ".whisper").mkdir(parents=True)
    text = json.dumps({"effort_level": "auto"})
    (ws / ".whisper" / "settings.json").write_text(text)
    seen = []
    monkeypatch.setattr(notifications, "_old_format_seen", set())
    monkeypatch.setattr(
        notifications,
        "record_notification_once",
        lambda **kw: seen.append((kw["title"], kw["message"])),
    )

    assert cfg.load_config(str(ws))["effort_level"] == "high"
    assert cfg.load_config(str(ws))["effort_level"] == "high"
    [(title, message)] = seen
    assert title == 'effort_level "auto" is no longer offered'
    assert str(ws / ".whisper" / "settings.json") in message
    assert "high effort is used" in message
    assert (ws / ".whisper" / "settings.json").read_text() == text

    # A project with a value the app offers says nothing.
    (ws / ".whisper" / "settings.json").write_text(json.dumps({"effort_level": "low"}))
    assert cfg.load_config(str(ws))["effort_level"] == "low"
    assert len(seen) == 1


def test_unknown_values_of_fixed_list_settings_get_a_notice(split_env, monkeypatch):
    from server.infrastructure.model_mode import current_mode

    tmp = split_env
    text = json.dumps(
        {
            "transcription_backend": " Parakeet",  # read as parakeet: known
            "translate_mode": "bogus",
            "model_mode": "offline",
        }
    )
    (tmp / "config.user.json").write_text(text)
    _reset(monkeypatch)

    [(title, message)] = cfg.old_config_notices()
    assert title == "Unknown setting values"
    assert 'translate_mode "bogus" (no translation line is shown)' in message
    assert 'model_mode "offline" (the app runs in cloud mode)' in message
    assert "transcription_backend" not in message
    # The notice says what the readers really do.
    assert current_mode(cfg.load_config()) == "cloud"
    assert (tmp / "config.user.json").read_text() == text

    (tmp / "config.user.json").write_text(json.dumps({"transcription_backend": "fast"}))
    [(_, message)] = cfg.old_config_notices()
    assert 'transcription_backend "fast" (Whisper transcribes)' in message


def test_startup_reports_old_settings_and_never_writes_the_user_config(monkeypatch):
    """Startup names every old setting it ignores and leaves both config.json
    and config.user.json byte for byte as they were."""
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    import server.main as main_mod
    import server.notifications as notifications

    legacy = Path(cfg.LEGACY_CONFIG_PATH)  # the conftest gives each test its own
    legacy.write_text('{"model_mode": "local"}')
    user = Path(cfg.USER_CONFIG_PATH)
    user_text = json.dumps(
        {
            "agent_limits": {"default": {"max_turns": 120}},
            "whisper_language": "pl,en",
            "transcription_backend": "canary",
            "chat_models": {
                "local_small": {
                    "id": "local:small",
                    "is_local": True,
                    "supports_tools": True,
                    "repo_id": "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF",
                    "filename": "Qwen2.5-Coder-1.5B-Instruct-Q4_K_M.gguf",
                }
            },
        },
        indent=4,
    )
    user.write_text(user_text)
    seen = []
    monkeypatch.setattr(notifications, "record_notification_once", lambda **kw: seen.append(kw))

    main_mod._report_old_config()

    assert [n["title"] for n in seen] == [
        "config.json is no longer read",
        "Old settings have no effect",
        'transcription_backend "canary" is no longer offered',
        "Small models still have tool calling on",
    ]
    assert {(n["source"], n["status"]) for n in seen} == {("config", "warning")}
    assert user.read_text() == user_text
    assert legacy.read_text() == '{"model_mode": "local"}'


def test_startup_shows_each_old_config_notice_once(monkeypatch):
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    import server.main as main_mod
    import server.notifications as notifications

    Path(cfg.LEGACY_CONFIG_PATH).write_text("{}")

    main_mod._report_old_config()
    main_mod._report_old_config()  # the next start

    rows = notifications.list_notifications()
    assert [(r["source"], r["title"], r["count"]) for r in rows] == [
        ("config", "config.json is no longer read", 1)
    ]
    # Cleared in the bell, it stays cleared on the next start.
    notifications.clear_all()
    main_mod._report_old_config()
    assert notifications.list_notifications() == []


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


# ── A config file that cannot be read fails, it never reads as empty ─────────


def test_a_malformed_user_config_raises_and_is_never_overwritten(tmp_path, monkeypatch):
    import pytest

    from server.infrastructure import config as cfg

    path = tmp_path / "config.user.json"
    broken = '{"bedrock_region": "eu-west-1", "chat_models": {"mine": {"id": "x"}},}'
    path.write_text(broken)
    monkeypatch.setattr(cfg, "_user_config_path", lambda: str(path))
    cfg._invalidate_cache()
    try:
        with pytest.raises(cfg.ConfigFileError, match="line 1"):
            cfg.load_config()
        with pytest.raises(cfg.ConfigFileError):
            cfg._apply_config_update({"brief_mode": True})
        assert path.read_text() == broken
    finally:
        cfg._invalidate_cache()


def test_a_user_config_that_is_not_an_object_raises(tmp_path, monkeypatch):
    import pytest

    from server.infrastructure import config as cfg

    path = tmp_path / "config.user.json"
    path.write_text("[]")
    monkeypatch.setattr(cfg, "_user_config_path", lambda: str(path))
    with pytest.raises(cfg.ConfigFileError, match="JSON object"):
        cfg._load_user_config()


def test_a_missing_user_config_is_an_empty_layer(tmp_path, monkeypatch):
    from server.infrastructure import config as cfg

    monkeypatch.setattr(cfg, "_user_config_path", lambda: str(tmp_path / "none.json"))
    assert cfg._load_user_config() == {}


def test_a_malformed_project_settings_file_raises(tmp_path):
    import pytest

    from server.infrastructure import config as cfg

    (tmp_path / ".whisper").mkdir()
    (tmp_path / ".whisper" / "settings.json").write_text("{nope")
    with pytest.raises(cfg.ConfigFileError, match="settings.json"):
        cfg._load_project_config(str(tmp_path))
    assert cfg._load_project_config(str(tmp_path / "elsewhere")) == {}


def test_a_missing_template_is_a_broken_install(tmp_path, monkeypatch):
    import pytest

    from server.infrastructure import config as cfg

    monkeypatch.setattr(cfg, "EXAMPLE_CONFIG_PATH", str(tmp_path / "config.example.json"))
    with pytest.raises(cfg.ConfigFileError, match="install is incomplete"):
        cfg._system_config()
