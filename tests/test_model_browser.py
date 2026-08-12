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


def test_param_count_b_reads_the_size_not_the_version():
    # A family version that looks like a size ("Qwen2.5") must not win over the
    # real parameter count later in the name.
    assert compat.param_count_b("Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF") == 1.5
    assert compat.param_count_b("bartowski/Llama-3.2-3B-Instruct-GGUF") == 3.0
    assert compat.param_count_b("google/gemma-4-12B-it-qat-q4_0-gguf") == 12.0
    # MoE reads as its TOTAL parameter count, not the active slice.
    assert compat.param_count_b("unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF") == 30.0
    # Falls back to the filename when the repo name carries no size.
    assert compat.param_count_b("some/mystery-gguf", "mystery-7B-Q4_K_M.gguf") == 7.0
    # A bit-width is not a parameter count (\b guards the suffix).
    assert compat.param_count_b("some/model-8bit-gguf") is None
    assert compat.param_count_b("some/random-repo") is None


def test_is_agentic_capable_gates_on_the_threshold():
    assert compat.is_agentic_capable("unsloth/Qwen3.5-9B-GGUF") is True
    assert compat.is_agentic_capable("google/gemma-4-12B-it-qat-q4_0-gguf") is True
    # Exactly at the threshold counts as capable.
    assert compat.is_agentic_capable("meta-llama/Llama-3.1-7B-Instruct-GGUF") is True
    assert compat.is_agentic_capable("Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF") is False
    assert compat.is_agentic_capable("unsloth/Qwen3-0.6B-GGUF") is False
    # Unknown size keeps the capability rather than silently downgrading.
    assert compat.is_agentic_capable("some/random-repo") is True


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


# ── compat: memory-fit verdict ───────────────────────────────────────────────
# Every case below states the machine's RAM explicitly and varies it — the
# same quant must verdict differently on a small machine than a large one.
# Nothing in fit_verdict is sized for any particular machine; only the inputs
# here are.
GIB = 1 << 30


def test_fit_verdict_same_quant_too_big_on_small_machine_ok_on_large_one():
    size_bytes = 18 * GIB  # roughly the Q4_K_M that triggered this feature
    assert compat.fit_verdict(size_bytes, 18 * GIB) == "too_big"
    assert compat.fit_verdict(size_bytes, 36 * GIB) == "ok"


def test_fit_verdict_tight_band_between_ok_and_too_big():
    # budget = 0.75 * 32 GiB = 24 GiB; tight starts at 0.85 * 24 GiB = 20.4 GiB
    # (needed = size + 2 GiB overhead).
    total = 32 * GIB
    assert compat.fit_verdict(10 * GIB, total) == "ok"  # needed 12 GiB, well under
    assert compat.fit_verdict(19 * GIB, total) == "tight"  # needed 21 GiB
    assert compat.fit_verdict(23 * GIB, total) == "too_big"  # needed 25 GiB > 24 GiB


def test_fit_verdict_reserved_bytes_can_tip_ok_into_too_big():
    total = 32 * GIB
    size_bytes = 10 * GIB  # needed 12 GiB, 'ok' against the full budget
    assert compat.fit_verdict(size_bytes, total, reserved_bytes=0) == "ok"
    # 20 GiB reserved by companion models leaves only a 4 GiB budget.
    assert compat.fit_verdict(size_bytes, total, reserved_bytes=20 * GIB) == "too_big"


@pytest.mark.parametrize(
    "size_bytes,total_mem_bytes",
    [(None, 32 * GIB), (0, 32 * GIB), (10 * GIB, None), (10 * GIB, 0)],
)
def test_fit_verdict_none_when_a_size_is_unknown(size_bytes, total_mem_bytes):
    assert compat.fit_verdict(size_bytes, total_mem_bytes) is None


# ── service: runtime memory measurement + companion reservation ─────────────
#
# Only the ACTIVE transcription backend (never both) plus the small speaker
# model are reserved. The workspace-index models (embed/GLiNER/GLiNER2/rerank)
# unload right after each indexing pass and are NOT counted — reserving their
# full on-disk size as if permanently resident was the reported bug: it could
# shrink the budget so far that a 6 GB model the machine easily runs, or even
# an already-installed working model, was graded "won't fit".
def test_reserved_companion_bytes_zero_when_no_companion_dirs_exist(tmp_path, monkeypatch):
    missing = str(tmp_path / "does-not-exist")
    monkeypatch.setattr(cfg, "load_config", lambda: {"transcription_backend": "whisper"})
    for target in (
        "server.asr.parakeet_backend.PARAKEET_MODEL_DIR",
        "server.asr.whisper_backend.WHISPER_MODEL_DIR",
        "server.diarization.speakers.SPEAKER_MODEL_DIR",
    ):
        monkeypatch.setattr(target, missing)
    service._reserved_companion_bytes.cache_clear()
    try:
        assert service._reserved_companion_bytes() == 0
    finally:
        service._reserved_companion_bytes.cache_clear()


def test_reserved_companion_bytes_counts_only_the_active_asr_backend(tmp_path, monkeypatch):
    """Both Parakeet and Whisper dirs may exist on disk (a user can switch
    backends), but only one is ever resident — reserving both was the other
    half of the over-counting bug."""
    missing = str(tmp_path / "does-not-exist")
    parakeet_dir = tmp_path / "parakeet"
    parakeet_dir.mkdir()
    (parakeet_dir / "weights.bin").write_bytes(b"x" * 5000)
    whisper_dir = tmp_path / "whisper"
    whisper_dir.mkdir()
    (whisper_dir / "weights.bin").write_bytes(b"y" * 9000)
    monkeypatch.setattr("server.asr.parakeet_backend.PARAKEET_MODEL_DIR", str(parakeet_dir))
    monkeypatch.setattr("server.asr.whisper_backend.WHISPER_MODEL_DIR", str(whisper_dir))
    monkeypatch.setattr("server.diarization.speakers.SPEAKER_MODEL_DIR", missing)

    monkeypatch.setattr(cfg, "load_config", lambda: {"transcription_backend": "streaming"})
    service._reserved_companion_bytes.cache_clear()
    try:
        assert service._reserved_companion_bytes() == 5000  # parakeet only ("streaming" alias)
    finally:
        service._reserved_companion_bytes.cache_clear()

    monkeypatch.setattr(cfg, "load_config", lambda: {"transcription_backend": "whisper"})
    service._reserved_companion_bytes.cache_clear()
    try:
        assert service._reserved_companion_bytes() == 9000  # whisper only
    finally:
        service._reserved_companion_bytes.cache_clear()


def test_reserved_companion_bytes_never_counts_index_models(tmp_path, monkeypatch):
    """embed/GLiNER/GLiNER2/rerank unload after each indexing pass and must
    never be added to the reservation, no matter how large they are on disk."""
    missing = str(tmp_path / "does-not-exist")
    monkeypatch.setattr(cfg, "load_config", lambda: {"transcription_backend": "whisper"})
    monkeypatch.setattr("server.asr.whisper_backend.WHISPER_MODEL_DIR", missing)
    monkeypatch.setattr("server.diarization.speakers.SPEAKER_MODEL_DIR", missing)
    big_index_model = tmp_path / "huge-index-model"
    big_index_model.mkdir()
    (big_index_model / "weights.bin").write_bytes(b"z" * 20_000)
    for target in (
        "server.index.config.EMBED_MODEL_DIR",
        "server.index.config.GLINER_MODEL_DIR",
        "server.index.config.GLINER2_MODEL_DIR",
        "server.index.config.RERANK_MODEL_DIR",
    ):
        monkeypatch.setattr(target, str(big_index_model))
    service._reserved_companion_bytes.cache_clear()
    try:
        assert service._reserved_companion_bytes() == 0
    finally:
        service._reserved_companion_bytes.cache_clear()


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
    # Rows carry the agentic verdict so Discover can badge a chat-only model
    # before the user commits to the download.
    assert all(r["tool_capable"] for r in results)


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
    # 0.6B: the detail view states the chat-only verdict and the threshold it
    # came from, so the UI quotes the server's rule instead of its own number.
    assert detail["tool_capable"] is False
    assert detail["param_size"] == "0.6B"
    assert detail["agentic_min_params_b"] == compat.AGENTIC_MIN_PARAMS_B


def test_repo_detail_quants_carry_fit_for_the_running_machine(monkeypatch):
    # Fixed at a small machine so a large quant clearly won't fit and a small
    # one clearly will — deterministic regardless of the machine running the
    # test suite.
    monkeypatch.setattr(service, "_total_memory_bytes", lambda: 18 * GIB)
    monkeypatch.setattr(service, "_reserved_companion_bytes", lambda: 0)
    monkeypatch.setattr(
        service.hf_client,
        "repo_gguf_meta",
        lambda repo_id: RepoGguf(arch="qwen3", context_length=40960, has_chat_template=True),
    )
    monkeypatch.setattr(
        service.hf_client,
        "repo_files",
        lambda repo_id: [
            ("Model-Q4_K_M.gguf", 5 * GIB),  # comfortably fits
            ("Model-Q8_0.gguf", 18 * GIB),  # exceeds an 18 GiB machine
        ],
    )
    monkeypatch.setattr(service.hf_client, "repo_is_gated", lambda repo_id: False)

    detail = service.repo_detail("someone/Model-GGUF")
    assert detail["mem_total_bytes"] == 18 * GIB
    assert detail["mem_reserved_bytes"] == 0
    assert detail["mem_budget_bytes"] == int(compat.MEM_BUDGET_FRACTION * 18 * GIB)
    by_quant = {q["quant"]: q for q in detail["quants"]}
    assert by_quant["Q4_K_M"]["fit"] == "ok"
    assert by_quant["Q8_0"]["fit"] == "too_big"


def test_repo_detail_omits_fit_when_memory_is_unknown(monkeypatch):
    monkeypatch.setattr(service, "_total_memory_bytes", lambda: None)
    monkeypatch.setattr(service, "_reserved_companion_bytes", lambda: 0)
    monkeypatch.setattr(
        service.hf_client,
        "repo_gguf_meta",
        lambda repo_id: RepoGguf(arch="qwen3", context_length=40960, has_chat_template=True),
    )
    monkeypatch.setattr(
        service.hf_client, "repo_files", lambda repo_id: [("Model-Q4_K_M.gguf", 5 * GIB)]
    )
    monkeypatch.setattr(service.hf_client, "repo_is_gated", lambda repo_id: False)

    detail = service.repo_detail("someone/Model-GGUF")
    assert detail["mem_total_bytes"] is None
    assert detail["mem_budget_bytes"] is None
    assert detail["quants"][0]["fit"] is None


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
    assert entry["supports_tools"] is False  # 0.6B is under the agentic threshold
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


def test_install_keeps_tools_on_for_a_model_above_the_threshold(isolated_home, monkeypatch):
    _stub_hf_and_queue(monkeypatch)
    result = service.install_model("unsloth/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf")
    data = json.loads((isolated_home / "config.user.json").read_text())
    assert data["chat_models"][result["key"]]["supports_tools"] is True
    assert result["supports_tools"] is True


def test_install_turns_tools_off_for_a_small_model(isolated_home, monkeypatch):
    # The failure this gate exists for: a 1.5B coder model answering a plain
    # greeting with a raw JSON tool call because it was handed the full pool.
    _stub_hf_and_queue(monkeypatch, arch="qwen2")
    result = service.install_model(
        "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF", "Qwen2.5-Coder-1.5B-Instruct-Q4_K_M.gguf"
    )
    data = json.loads((isolated_home / "config.user.json").read_text())
    assert data["chat_models"][result["key"]]["supports_tools"] is False
    # The install response carries it too, so the UI can say so in its toast.
    assert result["supports_tools"] is False


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


# ── one-shot sweep: models installed before the agentic gate existed ──────────
def _write_user_config(home, payload):
    (home / "config.user.json").write_text(json.dumps(payload))
    cfg._invalidate_cache()


def test_sweep_disables_tools_on_previously_installed_small_models(isolated_home):
    _write_user_config(
        isolated_home,
        {
            "chat_models": {
                "local_small": {
                    "id": "local:small",
                    "is_local": True,
                    "supports_tools": True,
                    "repo_id": "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF",
                    "filename": "Qwen2.5-Coder-1.5B-Instruct-Q4_K_M.gguf",
                },
                "local_big": {
                    "id": "local:big",
                    "is_local": True,
                    "supports_tools": True,
                    "repo_id": "unsloth/Qwen3.5-9B-GGUF",
                    "filename": "Qwen3.5-9B-Q4_K_M.gguf",
                },
                "opus5.0": {"id": "cloud-model", "supports_tools": True},
            }
        },
    )

    assert install.disable_tools_on_small_models() == ["local_small"]

    data = json.loads((isolated_home / "config.user.json").read_text())
    assert data["chat_models"]["local_small"]["supports_tools"] is False
    assert data["chat_models"]["local_big"]["supports_tools"] is True
    # A cloud model has no local tool loop and must never be touched by this.
    assert data["chat_models"]["opus5.0"]["supports_tools"] is True


def test_sweep_runs_once_so_a_deliberate_re_enable_sticks(isolated_home):
    _write_user_config(
        isolated_home,
        {
            "chat_models": {
                "local_small": {
                    "id": "local:small",
                    "is_local": True,
                    "supports_tools": True,
                    "repo_id": "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF",
                    "filename": "Qwen2.5-Coder-1.5B-Instruct-Q4_K_M.gguf",
                }
            }
        },
    )
    assert install.disable_tools_on_small_models() == ["local_small"]

    # The user turns it back on by hand — a supported choice.
    data = json.loads((isolated_home / "config.user.json").read_text())
    data["chat_models"]["local_small"]["supports_tools"] = True
    _write_user_config(isolated_home, data)

    assert install.disable_tools_on_small_models() == []
    data = json.loads((isolated_home / "config.user.json").read_text())
    assert data["chat_models"]["local_small"]["supports_tools"] is True


def test_sweep_leaves_an_entry_with_no_repo_id_alone(isolated_home):
    _write_user_config(
        isolated_home,
        {
            "chat_models": {
                "local_handrolled": {
                    "id": "local:handrolled",
                    "is_local": True,
                    "supports_tools": True,
                }
            }
        },
    )
    assert install.disable_tools_on_small_models() == []
    data = json.loads((isolated_home / "config.user.json").read_text())
    assert data["chat_models"]["local_handrolled"]["supports_tools"] is True


# ── recommended: the curated Discover catalog + one-click install ─────────────
def test_recommended_lists_curated_catalog():
    rows = service.recommended()
    keys = {r["key"] for r in rows}
    assert {"local_gemma", "local_gemma_coder", "local_qwen35_9b", "local_ministral3_14b"} <= keys
    gemma = next(r for r in rows if r["key"] == "local_gemma")
    assert gemma["repo_id"] == "google/gemma-4-12B-it-qat-q4_0-gguf"
    assert gemma["filename"].endswith(".gguf")
    assert "downloaded" in gemma
    # Every curated entry carries a real GGUF size, so Discover can warn before
    # a multi-gigabyte download rather than after it fails to run.
    assert gemma["size_bytes"] and gemma["size_bytes"] > 0


def test_recommended_rows_carry_fit_for_the_running_machine(monkeypatch):
    # A tiny machine: every curated model (all several GiB) should read too_big.
    monkeypatch.setattr(service, "_total_memory_bytes", lambda: 4 * GIB)
    monkeypatch.setattr(service, "_reserved_companion_bytes", lambda: 0)
    rows = service.recommended()
    gemma = next(r for r in rows if r["key"] == "local_gemma")
    assert gemma["fit"] == "too_big"

    # A generous machine: the same catalog should read ok.
    monkeypatch.setattr(service, "_total_memory_bytes", lambda: 64 * GIB)
    rows = service.recommended()
    gemma = next(r for r in rows if r["key"] == "local_gemma")
    assert gemma["fit"] == "ok"


def test_install_recommended_writes_canonical_entry_and_adopts_index_llm(
    isolated_home, monkeypatch
):
    _stub_hf_and_queue(monkeypatch)
    # Fresh-install seed: hybrid + index_llm=haiku (the cloud default).
    (isolated_home / "config.user.json").write_text(
        json.dumps({"model_mode": "hybrid", "backends": {"index_llm": "haiku"}})
    )
    cfg._invalidate_cache()

    out = service.install_recommended("local_gemma")
    assert out["key"] == "local_gemma"  # canonical key, not repo-derived
    assert out["adopted_index_llm"] is True

    data = json.loads((isolated_home / "config.user.json").read_text())
    entry = data["chat_models"]["local_gemma"]
    assert entry["is_local"] is True
    assert entry["repo_id"] == "google/gemma-4-12B-it-qat-q4_0-gguf"
    # First local install flips the index LLM to follow the on-device model.
    assert data["backends"]["index_llm"] == "local"


def test_install_recommended_does_not_override_explicit_index_llm(isolated_home, monkeypatch):
    _stub_hf_and_queue(monkeypatch)
    # A user who already chose a local index LLM must not be overridden.
    (isolated_home / "config.user.json").write_text(
        json.dumps({"model_mode": "hybrid", "backends": {"index_llm": "local_gemma_coder"}})
    )
    cfg._invalidate_cache()

    out = service.install_recommended("local_gemma")
    assert out["adopted_index_llm"] is False
    data = json.loads((isolated_home / "config.user.json").read_text())
    assert data["backends"]["index_llm"] == "local_gemma_coder"


def test_install_recommended_rejects_unknown_key(isolated_home, monkeypatch):
    with pytest.raises(service.InstallError):
        service.install_recommended("local_nonesuch")


# ── remove_from_list: local deleted outright vs Bedrock tombstoned ────────────
def test_remove_from_list_deletes_local_user_entry(isolated_home, monkeypatch):
    _stub_hf_and_queue(monkeypatch)
    result = service.install_model("unsloth/Qwen3-0.6B-GGUF", "Qwen3-0.6B-Q4_K_M.gguf")
    key = result["key"]

    # No files on disk in the test → _delete_weights short-circuits.
    monkeypatch.setattr("server.models_manager.catalog.get_entry", lambda k: None)
    out = service.remove_from_list(key)
    assert out["scope"] == "local"
    assert out["removed_entry"] is True

    data = json.loads((isolated_home / "config.user.json").read_text())
    # The user entry is gone, and NOTHING was added to the Bedrock hide-list.
    assert key not in data.get("chat_models", {})
    assert key not in data.get("chat_models_disabled", [])


def test_remove_from_list_deletes_a_downloaded_recommendation(isolated_home, monkeypatch):
    # A recommended model present on disk (e.g. from a prior release) with no user
    # config entry is still LOCAL — remove deletes it, never tombstones.
    monkeypatch.setattr(
        "server.local.registry._recommended_downloaded_on_disk",
        lambda entry: entry.get("id") == "local:gemma-4-12b-it-qat-q4_0",
    )
    monkeypatch.setattr("server.models_manager.catalog.get_entry", lambda k: None)
    out = service.remove_from_list("local_gemma")
    assert out["scope"] == "local"

    data = (
        json.loads((isolated_home / "config.user.json").read_text())
        if (isolated_home / "config.user.json").exists()
        else {}
    )
    assert "local_gemma" not in data.get("chat_models_disabled", [])


def test_remove_from_list_tombstones_bedrock_model(isolated_home, monkeypatch):
    # A Bedrock model (in the SYSTEM catalog, not local) has no weights and can't
    # be deleted, so it is hidden via the recoverable chat_models_disabled list.
    monkeypatch.setattr("server.models_manager.catalog.get_entry", lambda k: None)
    out = service.remove_from_list("opus5.0")
    assert out["scope"] == "bedrock"
    assert out["tombstoned"] is True

    data = json.loads((isolated_home / "config.user.json").read_text())
    assert "opus5.0" in data.get("chat_models_disabled", [])
    # The shipped entry itself is untouched (it lives in the system layer).
    assert "opus5.0" not in data.get("chat_models", {})


def test_tombstone_entry_is_idempotent_and_order_preserving(isolated_home, monkeypatch):
    assert install.tombstone_entry("opus5.0") is True
    # A second call is a no-op — no duplicate, returns False.
    assert install.tombstone_entry("opus5.0") is False
    install.tombstone_entry("sonnet5")
    data = json.loads((isolated_home / "config.user.json").read_text())
    assert data["chat_models_disabled"] == ["opus5.0", "sonnet5"]


def test_registry_key_is_stable_and_prefixed():
    key = install.registry_key("unsloth/Qwen3-0.6B-GGUF", "Q4_K_M")
    assert key == install.registry_key("unsloth/Qwen3-0.6B-GGUF", "Q4_K_M")
    assert key.startswith("local_")
    # Different quant → different key (so two quants of one repo coexist).
    assert key != install.registry_key("unsloth/Qwen3-0.6B-GGUF", "Q8_0")
