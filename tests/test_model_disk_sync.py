"""Local chat models in config.user.json and on disk are one set
(server/model_browser/disk_sync.py).

Runs the REAL config layer, registry, models catalog and download-manager
state against a throwaway user root: a weight file with no entry gets one, an
entry with no file (and no download) loses it, a delete from either tab takes
both, and Discover reports exactly the state Models shows.
"""

from __future__ import annotations

import json

import pytest

from server.infrastructure import config as cfg
from server.local.registry import RECOMMENDED_LOCAL_MODELS
from server.model_browser import disk_sync, install, service
from server.models_manager import catalog, manager

SYSTEM_CONFIG = {
    "bedrock_region": "us-east-1",
    "default_chat_model": "opus5.0",
    "model_mode": "local",
    "chat_models": {"opus5.0": {"id": "global.anthropic.claude-opus-5", "label": "Opus 5"}},
}

SEARCHED_REPO = "unsloth/Qwen3.8-27B-GGUF"
SEARCHED_FILE = "Qwen3.8-27B-UD-IQ1_M.gguf"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A user root of its own: models/ and config.user.json under tmp_path."""
    example = tmp_path / "config.example.json"
    example.write_text(json.dumps(SYSTEM_CONFIG))
    monkeypatch.setenv("WHISPER_USER_DIR", str(tmp_path))
    monkeypatch.setattr(cfg, "EXAMPLE_CONFIG_PATH", str(example))
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", str(tmp_path / "config.user.json"))
    monkeypatch.setattr(cfg, "_system_cache", None)
    monkeypatch.setattr(cfg, "_system_cache_key", None)
    cfg._invalidate_cache()
    (tmp_path / "models").mkdir()
    disk_sync.take_notices()
    yield tmp_path
    disk_sync.take_notices()
    cfg._invalidate_cache()


def _put(home, rel: str, data: bytes = b"x") -> None:
    path = home / "models" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _user_config(home) -> dict:
    path = home / "config.user.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _write_user_config(home, payload: dict) -> None:
    (home / "config.user.json").write_text(json.dumps(payload))
    cfg._invalidate_cache()


def _local_states() -> dict[str, str]:
    return {
        e.key: manager.state_of(e)[0]
        for e in catalog.entries()
        if e.group == catalog.GROUP_LOCAL_CHAT
    }


def _searched_entry() -> tuple[str, dict]:
    return install.build_entry(SEARCHED_REPO, SEARCHED_FILE, "UD-IQ1_M", None, None, None)


# ── weights with no entry are adopted ────────────────────────────────────────


def test_a_recommended_file_with_no_entry_gets_its_entry(home):
    rec = RECOMMENDED_LOCAL_MODELS["local_gemma"]
    _put(home, f"{rec['dir']}/{rec['filename']}")

    out = disk_sync.reconcile()

    assert [a["key"] for a in out["adopted"]] == ["local_gemma"]
    entry = _user_config(home)["chat_models"]["local_gemma"]
    assert (entry["dir"], entry["filename"]) == (rec["dir"], rec["filename"])
    assert _local_states() == {"local_gemma": "installed"}


def test_a_searched_install_file_with_no_entry_gets_the_entry_install_writes(home):
    key, expected = _searched_entry()
    _put(home, f"{expected['dir']}/{SEARCHED_FILE}")

    disk_sync.reconcile()

    entry = _user_config(home)["chat_models"][key]
    for field in ("id", "repo_id", "filename", "dir", "is_local"):
        assert entry[field] == expected[field]
    assert _local_states() == {key: "installed"}


def test_files_the_app_cannot_name_are_left_alone(home):
    _put(home, "my-own-folder/whatever-Q4_K_M.gguf")  # not a Discover folder name
    _put(home, "unsloth__Some-GGUF/no-quant-token.gguf")  # no quant to key it by
    _put(home, "unsloth__Big-GGUF/Big-Q4_K_M-00001-of-00002.gguf")  # shard 2 missing
    _put(home, "unsloth__Some-GGUF/mmproj-F16.gguf")  # a projector, not a chat model

    out = disk_sync.reconcile()

    assert out == {"adopted": [], "removed": []}
    assert "chat_models" not in _user_config(home)
    assert (home / "models/my-own-folder/whatever-Q4_K_M.gguf").exists()
    assert (home / "models/unsloth__Big-GGUF/Big-Q4_K_M-00001-of-00002.gguf").exists()


# ── entries with no weights are removed ──────────────────────────────────────


def test_an_entry_with_no_file_and_no_download_is_removed_with_its_hide_flag(home):
    key, entry = _searched_entry()
    _write_user_config(
        home,
        {"chat_models": {key: entry}, "chat_models_disabled": [key, "opus5.0"]},
    )

    out = disk_sync.reconcile()

    assert [r["key"] for r in out["removed"]] == [key]
    data = _user_config(home)
    assert key not in data.get("chat_models", {})
    assert data["chat_models_disabled"] == ["opus5.0"]
    assert [n["kind"] for n in disk_sync.take_notices()] == ["removed"]


def test_a_failed_download_is_removed_with_its_reason_and_partials(home, monkeypatch):
    key, entry = _searched_entry()
    _write_user_config(home, {"chat_models": {key: entry}})
    partial = f"{entry['dir']}/.cache/huggingface/download/{SEARCHED_FILE}.lock"
    _put(home, partial)
    monkeypatch.setitem(manager._errors, key, "Download failed: disk full")

    out = disk_sync.reconcile()

    assert out["removed"] == [
        {"key": key, "label": entry["label"], "reason": "Download failed: disk full"}
    ]
    assert not (home / "models" / partial).exists()
    assert key not in manager._errors


@pytest.mark.parametrize("state", ["downloading", "queued"])
def test_a_download_in_flight_keeps_its_entry(home, monkeypatch, state):
    key, entry = _searched_entry()
    _write_user_config(home, {"chat_models": {key: entry}})
    real = manager.state_of
    monkeypatch.setattr(manager, "state_of", lambda e: (state, None) if e.key == key else real(e))

    assert disk_sync.reconcile()["removed"] == []
    assert key in _user_config(home)["chat_models"]


def test_a_fetch_outside_the_manager_keeps_its_entry(home):
    key, entry = _searched_entry()
    _write_user_config(home, {"chat_models": {key: entry}})

    with disk_sync.fetching(key):
        assert disk_sync.reconcile()["removed"] == []
    assert key in _user_config(home)["chat_models"]
    assert [r["key"] for r in disk_sync.reconcile()["removed"]] == [key]


def test_removing_a_stale_quant_keeps_the_other_quant_in_its_folder(home):
    key, entry = _searched_entry()
    other = "Qwen3.8-27B-Q4_K_M.gguf"
    _put(home, f"{entry['dir']}/{other}")
    _write_user_config(home, {"chat_models": {key: entry}})

    out = disk_sync.reconcile()

    assert [r["key"] for r in out["removed"]] == [key]
    assert len(out["adopted"]) == 1
    assert (home / "models" / entry["dir"] / other).exists()


def test_a_second_reconcile_changes_nothing(home):
    rec = RECOMMENDED_LOCAL_MODELS["local_qwen35_9b"]
    _put(home, f"{rec['dir']}/{rec['filename']}")
    key, entry = _searched_entry()
    _write_user_config(home, {"chat_models": {key: entry}})

    disk_sync.reconcile()
    before = (home / "config.user.json").read_text()

    assert disk_sync.reconcile() == {"adopted": [], "removed": []}
    assert (home / "config.user.json").read_text() == before


# ── one delete, one state for both tabs ──────────────────────────────────────


def test_delete_takes_the_file_and_the_entry(home):
    rec = RECOMMENDED_LOCAL_MODELS["local_gemma"]
    _put(home, f"{rec['dir']}/{rec['filename']}")
    disk_sync.reconcile()
    _write_user_config(home, {**_user_config(home), "chat_models_disabled": ["local_gemma"]})

    service.remove_from_list("local_gemma")

    assert not (home / "models" / rec["dir"] / rec["filename"]).exists()
    data = _user_config(home)
    assert "local_gemma" not in data.get("chat_models", {})
    assert "local_gemma" not in data.get("chat_models_disabled", [])
    assert disk_sync.reconcile() == {"adopted": [], "removed": []}


def test_discover_recommended_reports_the_state_models_shows(home, monkeypatch):
    monkeypatch.setattr(service, "_kv_shape", lambda *a, **k: None)
    rec = RECOMMENDED_LOCAL_MODELS["local_ministral3_14b"]
    _put(home, f"{rec['dir']}/{rec['filename']}")

    rows = {r["key"]: r for r in service.recommended()}
    states = _local_states()

    for key, row in rows.items():
        assert row["downloaded"] == (states.get(key) == "installed"), key
    assert rows["local_ministral3_14b"]["downloaded"] is True


def test_search_rows_name_the_key_of_each_installed_quant(home):
    from server.model_browser import search

    key, entry = _searched_entry()
    _put(home, f"{entry['dir']}/{SEARCHED_FILE}")
    disk_sync.reconcile()

    ann = search._install_annotations("gguf")[SEARCHED_REPO]

    assert ann["installed"] == [SEARCHED_FILE]
    assert ann["keys"] == {SEARCHED_FILE: key}
