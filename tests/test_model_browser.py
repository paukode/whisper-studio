"""In-app model browser (server/model_browser/): compat rules, search filtering,
repo detail, and the P0 config-path install.

All Hugging Face calls are mocked — no network. The install tests run against a
throwaway WHISPER_HOME so they exercise the REAL config machinery and local
registry (the headline correctness claim: a browser install lands as a DICT
entry in ``chat_models`` — never ``chat_model_meta`` — and the registry picks it
up), while the download queue and the heavy models catalog are stubbed.
"""

from __future__ import annotations

import json

import pytest

from server.infrastructure import config as cfg
from server.model_browser import compat, install, service
from server.model_browser.hf_client import RepoGguf, SearchHit


# ── compat: architecture gate ────────────────────────────────────────────────
def test_arch_allowlist_accepts_supported_and_rejects_new():
    assert compat.arch_supported("qwen3")
    assert compat.arch_supported("qwen3moe")
    assert compat.arch_supported("gemma3")
    assert compat.arch_supported("llama")
    assert compat.arch_supported("PHI3")  # case-insensitive
    # Brand-new / hybrid archs the bundled engine can't load yet, and junk.
    assert not compat.arch_supported("qwen3next")
    assert not compat.arch_supported("mamba")
    assert not compat.arch_supported(None)
    assert not compat.arch_supported("")


# ── compat: quant parsing ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "filename,expected",
    [
        ("Qwen3-0.6B-Q4_K_M.gguf", "Q4_K_M"),
        ("Qwen3-0.6B-Q8_0.gguf", "Q8_0"),
        ("Qwen3-0.6B-Q2_K.gguf", "Q2_K"),
        ("Qwen3-0.6B-IQ4_XS.gguf", "IQ4_XS"),
        ("Qwen3-0.6B-UD-Q4_K_XL.gguf", "UD-Q4_K_XL"),
        ("Qwen3-0.6B-UD-IQ2_M.gguf", "UD-IQ2_M"),
        ("Model-BF16.gguf", "BF16"),
        ("Model-with-no-quant.gguf", None),
    ],
)
def test_parse_quant(filename, expected):
    assert compat.parse_quant(filename) == expected


def test_param_size_label():
    assert compat.param_size_label("unsloth/Qwen3-0.6B-GGUF") == "0.6B"
    assert compat.param_size_label("unsloth/Qwen3-8B-GGUF") == "8B"
    assert compat.param_size_label("unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF") == "30B"
    assert compat.param_size_label("some/random-repo") is None


def test_thinking_default():
    assert compat.is_thinking_model("qwen3", "unsloth/Qwen3-8B-GGUF")
    assert compat.is_thinking_model("llama", "org/DeepSeek-R1-Distill-Llama-8B-GGUF")
    assert not compat.is_thinking_model("gemma3", "google/gemma-3-1b-it-GGUF")
    assert not compat.is_thinking_model("llama", "meta/Llama-3.1-8B-Instruct-GGUF")


# ── compat: shard grouping + mmproj hiding + recommended ─────────────────────
def test_group_gguf_files_hides_mmproj_and_folds_shards():
    files = [
        ("Model-Q4_K_M.gguf", 400),
        ("Model-Q8_0.gguf", 800),
        ("Model-Q2_K.gguf", 200),
        ("mmproj-Model-F16.gguf", 999),  # vision projector — hidden
        ("Model-config.json", 10),  # non-gguf — ignored
        ("Model-nobase.gguf", 50),  # no quant token — skipped
        # A sharded quant: two files fold to ONE option, sizes summed.
        ("Big-Q5_K_M-00001-of-00002.gguf", 5000),
        ("Big-Q5_K_M-00002-of-00002.gguf", 4000),
    ]
    opts = compat.group_gguf_files(files)
    quants = {o.quant: o for o in opts}
    assert set(quants) == {"Q4_K_M", "Q8_0", "Q2_K", "Q5_K_M"}
    # mmproj + non-gguf + no-quant all excluded.
    assert not any("mmproj" in o.filename for o in opts)
    # Shards folded into one option with summed size and the FIRST shard kept.
    shard = quants["Q5_K_M"]
    assert shard.is_sharded and shard.shard_count == 2
    assert shard.size_bytes == 9000
    assert shard.filename == "Big-Q5_K_M-00001-of-00002.gguf"


def test_recommended_quant_prefers_q4_k_m():
    files = [
        ("M-Q2_K.gguf", 200),
        ("M-Q4_K_M.gguf", 400),
        ("M-Q5_K_M.gguf", 500),
        ("M-Q8_0.gguf", 800),
    ]
    opts = compat.group_gguf_files(files)
    assert compat.recommended_quant(opts) == "M-Q4_K_M.gguf"


def test_recommended_quant_falls_back_to_median():
    files = [("M-Q2_K.gguf", 200), ("M-Q3_K_S.gguf", 300), ("M-Q6_K.gguf", 600)]
    opts = compat.group_gguf_files(files)
    # No Q4_K_M/UD-Q4_K_XL/etc — median of three by size is Q3_K_S.
    assert compat.recommended_quant(opts) == "M-Q3_K_S.gguf"


# ── service.search: arch filtering, dedup, ranking ───────────────────────────
def test_search_filters_unsupported_archs_and_dedups(monkeypatch):
    hits = [
        SearchHit("unsloth/Qwen3-8B-GGUF", 300, 5, 3, "qwen3", 40960, False),
        SearchHit("unsloth/Qwen3-Coder-30B-GGUF", 500, 9, 7, "qwen3moe", 262144, False),
        SearchHit("someone/Qwen3-Next-GGUF", 999, 1, 9, "qwen3next", 262144, False),  # unsupported
        SearchHit("someone/Mystery-GGUF", 999, 1, 9, None, None, False),  # unknown arch
        SearchHit("unsloth/Qwen3-8B-GGUF", 300, 5, 3, "qwen3", 40960, False),  # duplicate
    ]
    monkeypatch.setattr(service.hf_client, "search", lambda *a, **k: list(hits))

    results = service.search(query="qwen3", sort="downloads", all_of_hf=True)
    ids = [r["repo_id"] for r in results]
    # qwen3 + qwen3moe kept; qwen3next + unknown dropped; duplicate collapsed.
    assert ids == ["unsloth/Qwen3-Coder-30B-GGUF", "unsloth/Qwen3-8B-GGUF"]
    assert all(r["supported"] for r in results)
    assert results[0]["downloads"] == 500  # ranked by downloads desc
    assert results[0]["param_size"] == "30B"
    assert results[0]["author"] == "unsloth"


def test_search_trusted_scope_queries_each_author(monkeypatch):
    calls: list[str | None] = []

    def fake_search(query, author, sort_key, limit):
        calls.append(author)
        return [SearchHit(f"{author}/Model-GGUF", 1, 1, 1, "llama", 4096, False)]

    monkeypatch.setattr(service.hf_client, "search", fake_search)
    results = service.search(query="mistral")  # default scope, no author
    # One call per trusted author, and every trusted author was queried.
    assert set(calls) == set(compat.TRUSTED_AUTHORS)
    assert len(results) == len(compat.TRUSTED_AUTHORS)


# ── service.repo_detail ──────────────────────────────────────────────────────
def test_repo_detail_shapes_quants(monkeypatch):
    monkeypatch.setattr(
        service.hf_client,
        "repo_gguf_meta",
        lambda repo_id: RepoGguf(arch="qwen3", context_length=40960, has_chat_template=True),
    )
    monkeypatch.setattr(
        service.hf_client,
        "repo_files",
        lambda repo_id: [
            ("Qwen3-0.6B-Q4_K_M.gguf", 400),
            ("Qwen3-0.6B-Q8_0.gguf", 800),
            ("mmproj-Qwen3-0.6B-F16.gguf", 999),
        ],
    )
    monkeypatch.setattr(service.hf_client, "repo_is_gated", lambda repo_id: False)

    detail = service.repo_detail("unsloth/Qwen3-0.6B-GGUF")
    assert detail["supported"] is True
    assert detail["arch"] == "qwen3"
    assert detail["context_length"] == 40960
    assert detail["has_chat_template"] is True
    quants = [q["quant"] for q in detail["quants"]]
    assert quants == ["Q4_K_M", "Q8_0"]  # mmproj hidden, size-sorted
    assert detail["recommended_filename"] == "Qwen3-0.6B-Q4_K_M.gguf"
    assert next(q for q in detail["quants"] if q["quant"] == "Q4_K_M")["recommended"] is True


# ── install: the config-path P0 (isolated real config + registry) ────────────
SYSTEM_CONFIG = {
    "bedrock_region": "us-east-1",
    "default_chat_model": "opus5.0",
    "model_mode": "local",
    "chat_models": {
        "opus5.0": {"id": "global.anthropic.claude-opus-5", "label": "Opus 5"},
    },
}


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Point the whole config layer at a throwaway home so install writes a real
    config.user.json the real registry then reads."""
    example = tmp_path / "config.example.json"
    example.write_text(json.dumps(SYSTEM_CONFIG))
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path))
    monkeypatch.setattr(cfg, "EXAMPLE_CONFIG_PATH", str(example))
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", str(tmp_path / "config.user.json"))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(cfg, "_system_cache", None)
    monkeypatch.setattr(cfg, "_system_cache_key", None)
    cfg._invalidate_cache()
    yield tmp_path
    cfg._invalidate_cache()


def _stub_hf_and_queue(monkeypatch, arch="qwen3", ctx=40960):
    """Make install network-free: a fixed GGUF header and a recording download
    queue that never spawns a worker."""
    monkeypatch.setattr(
        service.hf_client,
        "repo_gguf_meta",
        lambda repo_id: RepoGguf(arch=arch, context_length=ctx, has_chat_template=True),
    )
    started: list[str] = []

    def fake_start(entry):
        started.append(getattr(entry, "key", "?"))
        return "started"

    # get_entry is stubbed to (a) prove the registry picked the entry up and
    # (b) hand start_download a lightweight object instead of building the heavy
    # real catalog (which imports the ASR/index stacks).
    from server.local import registry

    class _Entry:
        def __init__(self, key):
            self.key = key

    def fake_get_entry(key):
        assert key in registry.local_models(), "registry did not pick up the config entry"
        return _Entry(key)

    monkeypatch.setattr("server.models_manager.manager.start_download", fake_start)
    monkeypatch.setattr("server.models_manager.catalog.get_entry", fake_get_entry)
    return started


def test_install_writes_dict_entry_into_chat_models(isolated_home, monkeypatch):
    started = _stub_hf_and_queue(monkeypatch, arch="qwen3", ctx=40960)

    result = service.install_model(
        repo_id="unsloth/Qwen3-0.6B-GGUF",
        filename="Qwen3-0.6B-Q4_K_M.gguf",
    )
    key = result["key"]
    assert result["download"] == "started"
    assert started == [key]  # the download was enqueued

    # The on-disk USER layer carries a DICT entry under chat_models — NEVER
    # chat_model_meta (the normalizer would clobber a meta-only entry).
    data = json.loads((isolated_home / "config.user.json").read_text())
    assert "chat_model_meta" not in data
    entry = data["chat_models"][key]
    assert isinstance(entry, dict)
    assert entry["is_local"] is True
    assert entry["id"].startswith("local:")
    assert entry["repo_id"] == "unsloth/Qwen3-0.6B-GGUF"
    assert entry["filename"] == "Qwen3-0.6B-Q4_K_M.gguf"
    assert entry["dir"] == "unsloth__Qwen3-0.6B-GGUF"
    assert entry["supports_tools"] is True
    assert entry["supports_thinking"] is True  # qwen3 → thinking default on
    assert entry["ctx"] == 32768  # header 40960 capped to the 32K default

    # The real registry resolves it with all required weight fields.
    from server.local.registry import local_models

    reg = local_models()
    assert key in reg
    assert reg[key]["repo_id"] == "unsloth/Qwen3-0.6B-GGUF"
    assert reg[key]["filename"] == "Qwen3-0.6B-Q4_K_M.gguf"


def test_install_merges_and_does_not_clobber_existing_user_entry(isolated_home, monkeypatch):
    # A pre-existing user entry must survive a new install.
    (isolated_home / "config.user.json").write_text(
        json.dumps({"chat_models": {"my_custom": {"id": "local:mine", "is_local": True}}})
    )
    cfg._invalidate_cache()
    _stub_hf_and_queue(monkeypatch)

    result = service.install_model("unsloth/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf")
    data = json.loads((isolated_home / "config.user.json").read_text())
    assert "my_custom" in data["chat_models"]
    assert result["key"] in data["chat_models"]


def test_install_refuses_unsupported_arch(isolated_home, monkeypatch):
    _stub_hf_and_queue(monkeypatch, arch="qwen3next")
    with pytest.raises(service.InstallError):
        service.install_model("someone/Qwen3-Next-GGUF", "model-Q4_K_M.gguf")
    # Nothing was written to the user config.
    assert not (isolated_home / "config.user.json").exists() or "chat_models" not in json.loads(
        (isolated_home / "config.user.json").read_text()
    )


def test_install_honours_explicit_n_ctx(isolated_home, monkeypatch):
    _stub_hf_and_queue(monkeypatch, arch="gemma3", ctx=131072)
    result = service.install_model(
        "unsloth/gemma-3-1b-it-GGUF", "gemma-3-1b-it-Q4_K_M.gguf", n_ctx=65536
    )
    data = json.loads((isolated_home / "config.user.json").read_text())
    assert data["chat_models"][result["key"]]["ctx"] == 65536
    assert result["supports_thinking"] is False  # gemma → not a thinking default


def test_uninstall_removes_user_entry(isolated_home, monkeypatch):
    _stub_hf_and_queue(monkeypatch)
    result = service.install_model("unsloth/Qwen3-0.6B-GGUF", "Qwen3-0.6B-Q4_K_M.gguf")
    key = result["key"]

    # Stub the manager so uninstall doesn't touch disk / llama-server.
    monkeypatch.setattr("server.models_manager.catalog.get_entry", lambda k: None)
    out = service.uninstall_model(key)
    assert out["removed"] is True
    data = json.loads((isolated_home / "config.user.json").read_text())
    assert key not in data.get("chat_models", {})


def test_uninstall_refuses_non_browser_key(isolated_home, monkeypatch):
    with pytest.raises(service.InstallError):
        service.uninstall_model("local_gemma")  # a built-in, not a user entry


# ── remove_from_list: user entry deleted vs shipped model tombstoned ──────────
def test_remove_from_list_deletes_user_entry(isolated_home, monkeypatch):
    _stub_hf_and_queue(monkeypatch)
    result = service.install_model("unsloth/Qwen3-0.6B-GGUF", "Qwen3-0.6B-Q4_K_M.gguf")
    key = result["key"]

    # No files on disk in the test → _delete_weights short-circuits.
    monkeypatch.setattr("server.models_manager.catalog.get_entry", lambda k: None)
    out = service.remove_from_list(key)
    assert out["scope"] == "user"
    assert out["removed_entry"] is True
    assert out["tombstoned"] is False

    data = json.loads((isolated_home / "config.user.json").read_text())
    # The user entry is gone, and NOTHING was added to the hide-list.
    assert key not in data.get("chat_models", {})
    assert key not in data.get("chat_models_disabled", [])


def test_remove_from_list_tombstones_shipped_model(isolated_home, monkeypatch):
    # A built-in / shipped key has no user-layer chat_models entry, so it can't
    # be deleted — remove_from_list must tombstone it into chat_models_disabled.
    monkeypatch.setattr("server.models_manager.catalog.get_entry", lambda k: None)
    out = service.remove_from_list("local_gemma")
    assert out["scope"] == "shipped"
    assert out["removed_entry"] is False
    assert out["tombstoned"] is True

    data = json.loads((isolated_home / "config.user.json").read_text())
    assert "local_gemma" in data.get("chat_models_disabled", [])
    # The shipped entry itself is untouched (it lives in the system layer).
    assert "local_gemma" not in data.get("chat_models", {})


def test_tombstone_entry_is_idempotent_and_order_preserving(isolated_home, monkeypatch):
    assert install.tombstone_entry("local_gemma") is True
    # A second call is a no-op — no duplicate, returns False.
    assert install.tombstone_entry("local_gemma") is False
    install.tombstone_entry("local_qwen")
    data = json.loads((isolated_home / "config.user.json").read_text())
    assert data["chat_models_disabled"] == ["local_gemma", "local_qwen"]


def test_registry_key_is_stable_and_prefixed():
    key = install.registry_key("unsloth/Qwen3-0.6B-GGUF", "Q4_K_M")
    assert key == install.registry_key("unsloth/Qwen3-0.6B-GGUF", "Q4_K_M")
    assert key.startswith("local_")
    # Different quant → different key (so two quants of one repo coexist).
    assert key != install.registry_key("unsloth/Qwen3-0.6B-GGUF", "Q8_0")
