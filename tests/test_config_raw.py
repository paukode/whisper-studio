"""Raw config.json editing (/api/config/raw): GET returns the literal file,
PUT validates — JSON syntax with line/column, then structure (every problem
reported at once, mirroring server/local/registry.py's local-model rules) —
writes atomically through the config module, and reports the post-save
local-model registry. The first save of a session snapshots a timestamped
backup next to the file (setup.sh's config.json.bak.<ts> convention).
"""

from __future__ import annotations

import glob
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure import config as config_mod
from server.infrastructure import config_raw_routes
from server.infrastructure.config_raw_routes import router

VALID_LOCAL_MODEL = {
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
    """Point the config module at a throwaway config.json and reset the
    per-session backup latch so each test starts fresh."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model_mode": "cloud"}, indent=2) + "\n")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config_raw_routes, "_backup_done", False)
    config_mod._invalidate_cache()
    yield path
    config_mod._invalidate_cache()


@pytest.fixture()
def client(cfg_path):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def put_text(client, text: str):
    return client.put("/api/config/raw", json={"text": text})


def backups(cfg_path) -> list[str]:
    return sorted(glob.glob(str(cfg_path) + ".bak.*"))


# ── GET ──────────────────────────────────────────────────────────────────


def test_get_returns_literal_text_and_path(client, cfg_path):
    body = client.get("/api/config/raw").json()
    assert body["text"] == cfg_path.read_text()
    assert body["path"] == str(cfg_path)


def test_get_missing_file_returns_empty_object(client, cfg_path):
    cfg_path.unlink()
    body = client.get("/api/config/raw").json()
    assert json.loads(body["text"]) == {}


# ── PUT: syntax validation ───────────────────────────────────────────────


def test_put_syntax_error_reports_line_and_column(client):
    r = put_text(client, '{\n  "model_mode": "cloud",\n  oops\n}\n')
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "line 3, column 3" in detail
    assert "JSON" in detail


def test_put_trailing_comma_is_a_syntax_error_not_a_500(client):
    r = put_text(client, '{"a": 1,}')
    assert r.status_code == 400
    assert "line 1" in r.json()["detail"]


def test_put_non_object_top_level_rejected(client):
    r = put_text(client, '["not", "an", "object"]')
    assert r.status_code == 400
    assert "top level" in r.json()["detail"].lower()


# ── PUT: structure validation (all problems at once) ─────────────────────


def test_put_reports_every_structural_problem_at_once(client):
    bad = {
        "model_mode": "turbo",  # not cloud/hybrid/local
        "bedrock_region": 7,  # not a string
        "chat_models": {
            "broken_local": {
                "id": "local:broken",
                "is_local": True,
                # repo_id present, filename and dir missing
                "repo_id": "acme/broken",
                "ctx": -5,  # not a positive integer
            },
            "empty_id": "",
        },
    }
    r = put_text(client, json.dumps(bad))
    assert r.status_code == 400
    lines = r.json()["detail"].split("\n")
    joined = "\n".join(lines)
    # One line per problem, each naming the exact entry/field.
    assert any('"model_mode"' in ln for ln in lines)
    assert any('"bedrock_region"' in ln for ln in lines)
    assert 'chat_models.broken_local: local model is missing a non-empty string "filename"' in joined
    assert 'chat_models.broken_local: local model is missing a non-empty string "dir"' in joined
    assert '"repo_id"' not in joined  # the present field is not flagged
    assert any("broken_local" in ln and '"ctx"' in ln for ln in lines)
    assert any("chat_models.empty_id" in ln for ln in lines)
    assert len(lines) >= 6


def test_put_local_model_without_id_is_flagged(client):
    entry = {k: v for k, v in VALID_LOCAL_MODEL.items() if k != "id"}
    r = put_text(client, json.dumps({"chat_models": {"tiny": entry}}))
    assert r.status_code == 400
    assert '"id"' in r.json()["detail"]


def test_put_local_model_id_must_have_local_prefix(client):
    entry = {**VALID_LOCAL_MODEL, "id": "acme.tiny-test"}
    r = put_text(client, json.dumps({"chat_models": {"tiny": entry}}))
    assert r.status_code == 400
    assert 'must start with "local:"' in r.json()["detail"]


def test_put_builtin_local_key_may_omit_weight_fields(client):
    """A config entry for a BUILT-IN local model (registry fills repo_id/
    filename/dir) may carry just an override like ctx."""
    text = json.dumps(
        {"chat_models": {"local_gemma": {"id": "local:gemma-4-12b-it-qat-q4_0", "is_local": True, "ctx": 16384}}}
    )
    r = put_text(client, text)
    assert r.status_code == 200, r.json()
    assert "local_gemma" in r.json()["local_models"]


def test_put_unknown_keys_are_allowed(client):
    r = put_text(client, '{"totally_new_setting": {"nested": true}}')
    assert r.status_code == 200


# ── PUT: successful save ─────────────────────────────────────────────────


def test_put_valid_config_persists_and_registry_sees_the_model(client, cfg_path):
    text = json.dumps(
        {"model_mode": "local", "chat_models": {"tiny_local": VALID_LOCAL_MODEL}}, indent=2
    )
    r = put_text(client, text)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # The response reflects the POST-save registry: built-ins plus the new key.
    assert "tiny_local" in body["local_models"]
    assert "local_gemma" in body["local_models"]
    # Written verbatim (plus the normalized trailing newline).
    assert cfg_path.read_text() == text + "\n"
    # The live registry (what the models catalog reads) picked it up too.
    from server.local.registry import local_models

    reg = local_models()
    assert reg["tiny_local"]["repo_id"] == "acme/tiny-test-gguf"
    assert reg["tiny_local"]["filename"] == "tiny-test-Q4_K_M.gguf"
    # And the merged config sees the new mode without a restart.
    assert config_mod.load_config()["model_mode"] == "local"


def test_put_rejected_save_leaves_the_file_untouched(client, cfg_path):
    before = cfg_path.read_text()
    assert put_text(client, "{nope").status_code == 400
    assert put_text(client, '{"model_mode": "warp"}').status_code == 400
    assert cfg_path.read_text() == before


# ── backup safety rail ───────────────────────────────────────────────────


def test_first_save_creates_one_timestamped_backup(client, cfg_path):
    original = cfg_path.read_text()
    assert put_text(client, '{"model_mode": "hybrid"}').status_code == 200
    baks = backups(cfg_path)
    assert len(baks) == 1
    with open(baks[0]) as f:
        assert f.read() == original  # the PRE-save contents
    # Second save in the same session: no new backup.
    assert put_text(client, '{"model_mode": "cloud"}').status_code == 200
    assert backups(cfg_path) == baks


def test_no_backup_when_no_file_existed(client, cfg_path):
    cfg_path.unlink()
    assert put_text(client, '{"model_mode": "cloud"}').status_code == 200
    assert backups(cfg_path) == []
    assert json.loads(cfg_path.read_text())["model_mode"] == "cloud"
