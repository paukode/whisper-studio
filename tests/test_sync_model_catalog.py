"""setup.sh's add-only model-catalog sync: a model added to
config.example.json reaches an existing config.json on the next setup run
(no --new needed), while user-edited entries are never touched."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from sync_model_catalog import main, sync_chat_models, sync_pricing  # noqa: E402


def _write(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def test_new_template_model_is_appended(tmp_path):
    cfg = tmp_path / "config.json"
    tpl = tmp_path / "config.example.json"
    _write(cfg, {"chat_models": {"haiku": {"id": "h"}, "local_gemma": {"id": "local:g"}}})
    _write(
        tpl,
        {
            "chat_models": {
                "haiku": {"id": "h"},
                "local_gemma": {"id": "local:g"},
                "local_qwen": {"id": "local:q", "repo_id": "r", "filename": "f", "dir": "d"},
            }
        },
    )
    added = sync_chat_models(str(cfg), str(tpl))
    assert added == ["local_qwen"]
    out = json.load(open(cfg))
    assert out["chat_models"]["local_qwen"]["repo_id"] == "r"
    # A .bak snapshot of the pre-sync file exists.
    baks = [p for p in os.listdir(tmp_path) if p.startswith("config.json.bak.")]
    assert len(baks) == 1


def test_user_edits_and_extra_keys_survive(tmp_path):
    cfg = tmp_path / "config.json"
    tpl = tmp_path / "config.example.json"
    _write(
        cfg,
        {
            "tavily_api_key": "secret",
            "chat_models": {
                "haiku": {"id": "h", "label": "My renamed Haiku", "ctx": 999},
                "my_custom": {"id": "local:custom"},
            },
        },
    )
    _write(tpl, {"chat_models": {"haiku": {"id": "h", "label": "Haiku 4.5"}, "new": {"id": "n"}}})
    added = sync_chat_models(str(cfg), str(tpl))
    assert added == ["new"]
    out = json.load(open(cfg))
    # The user's override of an existing key is untouched; their extra key stays.
    assert out["chat_models"]["haiku"]["label"] == "My renamed Haiku"
    assert out["chat_models"]["haiku"]["ctx"] == 999
    assert "my_custom" in out["chat_models"]
    assert out["tavily_api_key"] == "secret"


def test_noop_when_in_sync_writes_nothing(tmp_path):
    cfg = tmp_path / "config.json"
    tpl = tmp_path / "config.example.json"
    _write(cfg, {"chat_models": {"haiku": {"id": "h"}}})
    _write(tpl, {"chat_models": {"haiku": {"id": "h"}}})
    before = cfg.read_text()
    assert sync_chat_models(str(cfg), str(tpl)) == []
    assert cfg.read_text() == before
    assert not [p for p in os.listdir(tmp_path) if ".bak." in p]


def test_corrupt_or_missing_files_change_nothing(tmp_path):
    cfg = tmp_path / "config.json"
    tpl = tmp_path / "config.example.json"
    cfg.write_text("{not json")
    _write(tpl, {"chat_models": {"haiku": {"id": "h"}}})
    assert sync_chat_models(str(cfg), str(tpl)) == []
    assert cfg.read_text() == "{not json"  # untouched
    # Missing config entirely: nothing to sync into (first-run seed covers it).
    assert sync_chat_models(str(tmp_path / "nope.json"), str(tpl)) == []


def test_empty_user_catalog_left_to_runtime_seed(tmp_path):
    """A config with no chat_models falls back to the template at runtime
    (load_config seeds it), so the sync must not materialize a catalog."""
    cfg = tmp_path / "config.json"
    tpl = tmp_path / "config.example.json"
    _write(cfg, {"bedrock_region": "us-east-1"})
    _write(tpl, {"chat_models": {"haiku": {"id": "h"}}})
    assert sync_chat_models(str(cfg), str(tpl)) == []
    assert "chat_models" not in json.load(open(cfg))


def test_pricing_sync_appends_new_keys_only(tmp_path):
    pricing = tmp_path / "pricing.json"
    tpl = tmp_path / "pricing.example.json"
    _write(pricing, {"haiku": {"in": 1, "out": 5}})
    _write(tpl, {"haiku": {"in": 2, "out": 6}, "gpt6": {"in": 3, "out": 9}})
    assert sync_pricing(str(pricing), str(tpl)) == ["gpt6"]
    out = json.load(open(pricing))
    assert out["haiku"] == {"in": 1, "out": 5}  # user's prices untouched
    assert out["gpt6"] == {"in": 3, "out": 9}


def test_main_end_to_end(tmp_path, capsys):
    _write(tmp_path / "config.json", {"chat_models": {"haiku": {"id": "h"}}})
    _write(
        tmp_path / "config.example.json",
        {"chat_models": {"haiku": {"id": "h"}, "local_new": {"id": "local:n"}}},
    )
    _write(tmp_path / "pricing.json", {"haiku": {}})
    _write(tmp_path / "pricing.example.json", {"haiku": {}})
    assert main(str(tmp_path)) == 0
    assert "local_new" in capsys.readouterr().out
    assert "local_new" in json.load(open(tmp_path / "config.json"))["chat_models"]
