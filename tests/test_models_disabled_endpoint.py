"""PUT/GET /api/config/models-disabled — the dedicated persistence path behind
the Settings per-model visibility toggles.

GET returns the user's hide-list plus the FULL merged SYSTEM+USER catalog
(including currently disabled models, or the UI could never re-enable one),
each row flagged. PUT validates a list of strings (unknown keys warn, they
don't fail — the raw editor's policy), writes atomically through the config
module's structured save into config.user.json, and responds with the
post-save list. Disabling only hides a model from the composer picker: the
local registry (and with it the download manager) still serves it so its
weights stay manageable.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure import config as config_mod
from server.infrastructure import config_raw_routes
from server.infrastructure.config_raw_routes import router

TINY_LOCAL = {
    "id": "local:tiny-test-model",
    "label": "Tiny Test (Local)",
    "is_local": True,
    "repo_id": "acme/tiny-test-gguf",
    "filename": "tiny-test-Q4_K_M.gguf",
    "dir": "tiny-test",
    "ctx": 8192,
}


@pytest.fixture()
def cfg_path(tmp_path, monkeypatch):
    """Throwaway config.user.json as the active USER layer (legacy config.json
    aimed at a nonexistent path), on top of the real SYSTEM catalog."""
    path = tmp_path / "config.user.json"
    path.write_text(json.dumps({"model_mode": "cloud"}, indent=2) + "\n")
    monkeypatch.setattr(config_mod, "USER_CONFIG_PATH", str(path))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config_raw_routes, "_backup_done", False)
    config_mod._invalidate_cache()
    yield path
    config_mod._invalidate_cache()


@pytest.fixture()
def client(cfg_path):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def put_disabled(client, body):
    return client.put("/api/config/models-disabled", json=body)


def user_cfg(cfg_path) -> dict:
    return json.loads(cfg_path.read_text())


# ── GET ──────────────────────────────────────────────────────────────────


def test_get_lists_full_catalog_all_enabled_by_default(client):
    body = client.get("/api/config/models-disabled").json()
    assert body["disabled"] == []
    keys = {m["key"] for m in body["models"]}
    # Cloud and local chat models from the SYSTEM catalog are all present …
    assert {"opus4.8", "haiku", "local_gemma"} <= keys
    # … every one visible (all on by default), with usable labels + local flag.
    assert all(m["disabled"] is False for m in body["models"])
    by_key = {m["key"]: m for m in body["models"]}
    assert by_key["local_gemma"]["is_local"] is True
    assert by_key["opus4.8"]["is_local"] is False
    assert by_key["opus4.8"]["label"]


def test_get_includes_disabled_models_flagged(client, cfg_path):
    cfg_path.write_text(json.dumps({"chat_models_disabled": ["haiku"]}))
    config_mod._invalidate_cache()
    body = client.get("/api/config/models-disabled").json()
    by_key = {m["key"]: m for m in body["models"]}
    # A hidden model STAYS in this list (else it could never be re-enabled).
    assert by_key["haiku"]["disabled"] is True
    assert body["disabled"] == ["haiku"]


def test_get_includes_user_added_models(client, cfg_path):
    cfg_path.write_text(json.dumps({"chat_models": {"tiny_local": TINY_LOCAL}}))
    config_mod._invalidate_cache()
    body = client.get("/api/config/models-disabled").json()
    by_key = {m["key"]: m for m in body["models"]}
    assert by_key["tiny_local"]["is_local"] is True
    assert by_key["tiny_local"]["label"] == "Tiny Test (Local)"


# ── PUT: validation ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {"disabled": "haiku"},  # string, not a list
        {"disabled": ["haiku", 7]},  # non-string entry
        {"disabled": None},
        {},  # key missing entirely
        ["haiku"],  # top level not an object
    ],
)
def test_put_rejects_non_string_lists(client, cfg_path, body):
    before = cfg_path.read_text()
    r = put_disabled(client, body)
    assert r.status_code == 400
    assert "disabled" in r.json()["detail"]
    assert cfg_path.read_text() == before  # rejected saves never touch the file


def test_put_unknown_key_warns_but_saves(client, cfg_path, caplog):
    # Unknown names warn (typo / pre-disabling a future model), never fail.
    with caplog.at_level("WARNING", logger="whisper-studio"):
        r = put_disabled(client, {"disabled": ["not_a_real_model"]})
    assert r.status_code == 200
    assert "not_a_real_model" in caplog.text
    assert user_cfg(cfg_path)["chat_models_disabled"] == ["not_a_real_model"]


# ── PUT: persistence + effective list ────────────────────────────────────


def test_put_persists_into_user_config_and_flags_the_row(client, cfg_path):
    r = put_disabled(client, {"disabled": ["haiku", "sonnet"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["disabled"] == ["haiku", "sonnet"]
    by_key = {m["key"]: m for m in body["models"]}
    # The response is the POST-save list: hidden rows flagged, still present.
    assert by_key["haiku"]["disabled"] is True
    assert by_key["sonnet"]["disabled"] is True
    assert by_key["opus4.8"]["disabled"] is False
    # Persisted in the USER layer, other keys untouched.
    on_disk = user_cfg(cfg_path)
    assert on_disk["chat_models_disabled"] == ["haiku", "sonnet"]
    assert on_disk["model_mode"] == "cloud"
    # The effective catalog (what /api/models reads) drops them immediately.
    effective = config_mod.load_config()["chat_models"]
    assert "haiku" not in effective
    assert "sonnet" not in effective
    assert "opus4.8" in effective


def test_put_dedupes_preserving_order(client, cfg_path):
    r = put_disabled(client, {"disabled": ["sonnet", "haiku", "sonnet"]})
    assert r.status_code == 200
    assert user_cfg(cfg_path)["chat_models_disabled"] == ["sonnet", "haiku"]


def test_put_empty_list_reenables_everything_and_drops_the_key(client, cfg_path):
    assert put_disabled(client, {"disabled": ["haiku"]}).status_code == 200
    assert "haiku" not in config_mod.load_config()["chat_models"]

    r = put_disabled(client, {"disabled": []})
    assert r.status_code == 200
    assert all(m["disabled"] is False for m in r.json()["models"])
    # The key is dropped so the user layer stays minimal.
    assert "chat_models_disabled" not in user_cfg(cfg_path)
    assert "haiku" in config_mod.load_config()["chat_models"]


def test_toggle_roundtrip_matches_raw_editor_key(client, cfg_path):
    """The toggles and the raw editor write the SAME key: a hide-list saved via
    the raw editor shows up in GET, and a toggle change is visible to the raw
    editor's next read."""
    assert (
        client.put(
            "/api/config/raw", json={"text": json.dumps({"chat_models_disabled": ["opus4.6"]})}
        ).status_code
        == 200
    )
    body = client.get("/api/config/models-disabled").json()
    assert body["disabled"] == ["opus4.6"]

    assert put_disabled(client, {"disabled": []}).status_code == 200
    raw = client.get("/api/config/raw").json()
    assert "chat_models_disabled" not in json.loads(raw["user_text"])


# ── disabled models keep manageable weights ──────────────────────────────


def test_disabled_local_model_stays_in_registry(client, cfg_path):
    """Disabling hides a model from the picker (the filtered catalog) but the
    local registry — the source of the download manager — must keep it, or a
    disabled model's weights could never be downloaded or deleted again."""
    cfg_path.write_text(json.dumps({"chat_models": {"tiny_local": TINY_LOCAL}}))
    config_mod._invalidate_cache()

    r = put_disabled(client, {"disabled": ["tiny_local", "local_gemma"]})
    assert r.status_code == 200

    from server.local.registry import local_models

    reg = local_models()
    # Both the user-added model and the shipped one stay registered …
    assert reg["tiny_local"]["repo_id"] == "acme/tiny-test-gguf"
    assert "local_gemma" in reg
    # … while the picker catalog no longer offers them.
    effective = config_mod.load_config()["chat_models"]
    assert "tiny_local" not in effective
    assert "local_gemma" not in effective
