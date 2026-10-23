"""The memory-fit grade's KV-cache estimate (server/model_browser/gguf_kv.py).

llama-server allocates the KV cache for the whole context at load, so the
grade adds the model's own cache at the context the app runs it with instead
of a flat allowance. These tests pin how the estimate relates across models
and contexts, never a model's byte count: headers are built in memory, and no
test reaches Hugging Face (conftest refuses the remote read; the tests that
exercise it stub HfFileSystem).
"""

from __future__ import annotations

import io
import os
import struct

import pytest

from server.model_browser import compat, gguf_kv, service
from server.model_browser.hf_client import RepoGguf
from server.model_browser.hf_client import gguf_file_metadata as real_gguf_file_metadata
from tests.gguf_headers import gguf_bytes, header

GIB = 1 << 30
CTX = 32768


def kv(meta: dict, n_ctx: int = CTX) -> int:
    shape = gguf_kv.kv_shape(meta)
    assert shape is not None
    return gguf_kv.kv_cache_bytes(shape, n_ctx)


# ── header reader ────────────────────────────────────────────────────────────
def test_reader_round_trips_scalars_strings_and_per_layer_arrays():
    meta = header(arch="gemma4", layers=6, kv_heads=[8, 8, 1, 8, 8, 1])
    meta["general.name"] = "Test"
    meta["gemma4.rope.freq_base"] = 10000.0
    read = gguf_kv.read_metadata(io.BytesIO(gguf_bytes(meta)))
    assert read["gemma4.attention.head_count_kv"] == [8, 8, 1, 8, 8, 1]
    assert read["general.name"] == "Test"
    assert read["gemma4.block_count"] == 6
    assert read["gemma4.rope.freq_base"] == pytest.approx(10000.0)


def test_reader_stops_at_the_vocabulary_and_skips_long_numeric_arrays():
    """The model keys come before the tokenizer, so a remote read never has to
    fetch the megabytes of vocabulary. A long numeric array costs a seek, not
    a read, and the keys after it still count."""
    meta = header()
    meta["tokenizer.ggml.scores"] = [0.5] * 5000
    meta["llama.after_scores"] = 7
    meta["tokenizer.ggml.tokens"] = [f"t{i}" for i in range(5000)]
    meta["llama.after_vocab"] = 1
    data = gguf_bytes(meta)
    f = io.BytesIO(data)
    read = gguf_kv.read_metadata(f)
    assert read["llama.after_scores"] == 7
    assert "tokenizer.ggml.scores" not in read
    assert "llama.after_vocab" not in read
    assert f.tell() < len(data) // 4  # the vocabulary itself was not read
    assert gguf_kv.kv_shape(read) == gguf_kv.kv_shape(header())


@pytest.mark.parametrize(
    "data", [b"", b"GGML" + bytes(20), b"GGUF" + struct.pack("<I", 1) + bytes(16), b"GGUF\x03\x00"]
)
def test_reader_rejects_bytes_that_are_not_a_gguf_header(data):
    with pytest.raises(gguf_kv.GgufError):
        gguf_kv.read_metadata(io.BytesIO(data))


# ── KV-cache size: relations ─────────────────────────────────────────────────
def test_more_full_attention_layers_need_more():
    assert kv(header(layers=48)) > kv(header(layers=40)) > kv(header(layers=32))


def test_more_kv_heads_or_wider_heads_need_more():
    assert kv(header(kv_heads=8)) > kv(header(kv_heads=4))
    assert kv(header(head_dim=256)) > kv(header(head_dim=128))


def test_a_larger_context_needs_more():
    meta = header()
    assert kv(meta, 65536) > kv(meta, 32768) > kv(meta, 16384)


def test_a_full_attention_cache_grows_in_step_with_the_context():
    meta = header()
    assert kv(meta, 2 * CTX) == 2 * kv(meta, CTX)


def test_sliding_window_layers_need_less_than_full_attention():
    full = header(arch="gemma4", layers=12)
    windowed = header(
        arch="gemma4",
        layers=12,
        sliding_window=1024,
        sliding_window_pattern=[True] * 5 + [False] + [True] * 5 + [False],
    )
    assert kv(windowed) < kv(full)


def test_window_layers_stop_growing_once_the_context_passes_the_window():
    """Only the full-attention layers grow with the context; doubling it adds
    exactly what the same full-attention layers alone add."""
    pattern = [True] * 5 + [False] + [True] * 5 + [False]
    windowed = header(arch="gemma4", layers=12, sliding_window=1024, sliding_window_pattern=pattern)
    full_only = header(arch="gemma4", layers=2)
    assert kv(windowed, 2 * CTX) - kv(windowed, CTX) == kv(full_only, 2 * CTX) - kv(full_only, CTX)


def test_window_layers_can_have_their_own_head_size():
    pattern = [True, True, False]
    small = header(arch="gemma4", layers=3, sliding_window=1024, sliding_window_pattern=pattern)
    wide = dict(small)
    wide["gemma4.attention.key_length_swa"] = 512
    wide["gemma4.attention.value_length_swa"] = 512
    assert kv(wide) > kv(small)


def test_a_family_with_a_fixed_window_layout_needs_less_than_full_attention():
    """Gemma 3 headers carry the window but not the layer pattern: llama.cpp
    fixes it per family, and so does the estimate."""
    gemma3 = header(arch="gemma3", layers=48, sliding_window=1024)
    assert kv(gemma3) < kv(header(arch="gemma3", layers=48))


def test_an_unknown_window_layout_never_counts_for_less_than_full_attention():
    unknown = header(arch="newarch", layers=40, sliding_window=4096)
    assert kv(unknown) == kv(header(arch="newarch", layers=40))


def test_hybrid_recurrent_layers_need_less_than_all_attention():
    hybrid = header(arch="qwen35", layers=32, full_attention_interval=4)
    dense = header(arch="qwen35", layers=32)
    assert kv(hybrid) < kv(dense)
    # Every fourth layer keeps a cache: the same as a model of a quarter the depth.
    assert kv(hybrid) == kv(header(arch="qwen35", layers=8))


def test_layers_without_kv_heads_or_with_shared_kv_cost_nothing():
    zeros = header(layers=4, kv_heads=[8, 0, 8, 0])
    assert kv(zeros) == kv(header(layers=2))
    shared = header(arch="gemma3n", layers=10, shared_kv_layers=4)
    assert kv(shared) == kv(header(arch="gemma3n", layers=6))


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"general.architecture": "llama"},
        {"general.architecture": "llama", "llama.block_count": 40},
        header(kv_heads=[8, 8]),  # a per-layer list that does not match the layers
    ],
)
def test_a_header_without_the_attention_keys_gives_no_shape(meta):
    assert gguf_kv.kv_shape(meta) is None


# ── context checkpoint size: relations ───────────────────────────────────────
def hybrid(interval=4, **ssm) -> dict:
    """A Qwen3.5-like hybrid: every ``interval``-th layer attends, the others
    are recurrent with the given ``ssm.*`` keys."""
    meta = header(arch="qwen35", layers=32, full_attention_interval=interval)
    keys = {"conv_kernel": 4, "state_size": 128, "group_count": 16, "inner_size": 4096, **ssm}
    meta.update({f"qwen35.ssm.{k}": v for k, v in keys.items()})
    return meta


def checkpoint(meta: dict) -> int:
    shape = gguf_kv.kv_shape(meta)
    assert shape is not None
    return gguf_kv.checkpoint_bytes(shape)


def test_a_full_attention_model_has_no_checkpoint_state():
    assert checkpoint(header()) == 0


def test_a_window_checkpoint_grows_with_the_window():
    pattern = [True, True, False]
    small = header(arch="gemma4", layers=3, sliding_window=1024, sliding_window_pattern=pattern)
    large = header(arch="gemma4", layers=3, sliding_window=2048, sliding_window_pattern=pattern)
    assert checkpoint(large) > checkpoint(small) > 0


def test_a_hybrid_checkpoint_is_its_recurrent_state():
    assert checkpoint(hybrid(state_size=256)) > checkpoint(hybrid(state_size=128)) > 0
    # More recurrent layers (fewer attention layers) keep more state.
    assert checkpoint(hybrid(interval=8)) > checkpoint(hybrid(interval=4))
    # The recurrent state does not depend on the context, so it is not KV cache.
    assert kv(hybrid()) == kv(header(arch="qwen35", layers=32, full_attention_interval=4))


def test_recurrent_layers_without_ssm_keys_count_no_state():
    assert checkpoint(header(arch="qwen35", layers=32, full_attention_interval=4)) == 0


# ── the fit grade ────────────────────────────────────────────────────────────
def test_a_larger_kv_cache_needs_more_memory_and_can_tip_the_grade():
    size, total = 8 * GIB, 18 * GIB
    small, large = kv(header(layers=8)), kv(header(layers=40))
    assert compat.needed_bytes(size, large) > compat.needed_bytes(size, small) > size
    assert compat.fit_verdict(size, total, 0, small) == "ok"
    assert compat.fit_verdict(size, total, 0, large) == "too_big"


def test_an_unknown_kv_cache_keeps_the_flat_allowance():
    size = 8 * GIB
    assert compat.needed_bytes(size) == compat.needed_bytes(size, None)
    assert compat.needed_bytes(size, None) == size + compat.RUNTIME_OVERHEAD_BYTES


# ── service: the header source and the context it is graded at ──────────────
def _stub_repo(monkeypatch, headers: dict[str, dict | None], ctx=262144):
    """A one-quant repo per repo id, all the same size; ``headers`` maps the
    repo id to its GGUF header (None = unreadable). Returns the read log."""
    reads: list[str] = []
    monkeypatch.setattr(service, "_total_memory_bytes", lambda: 18 * GIB)
    monkeypatch.setattr(service, "_reserved_companion_bytes", lambda: 0)
    monkeypatch.setattr(
        service.hf_client,
        "repo_gguf_meta",
        lambda repo_id: RepoGguf(arch="llama", context_length=ctx, has_chat_template=True),
    )
    monkeypatch.setattr(
        service.hf_client, "repo_files", lambda repo_id: [("Model-Q4_K_M.gguf", 8 * GIB)]
    )
    monkeypatch.setattr(service.hf_client, "repo_is_gated", lambda repo_id: False)

    def read(repo_id, filename):
        reads.append(repo_id)
        return headers[repo_id]

    monkeypatch.setattr(service.hf_client, "gguf_file_metadata", read)
    return reads


def _needed(detail: dict) -> int:
    return detail["quants"][0]["needed_bytes"]


def test_repo_detail_grades_each_repo_by_its_own_kv_cache(monkeypatch):
    _stub_repo(
        monkeypatch,
        {
            "a/Deep-GGUF": header(layers=40),
            "a/Hybrid-GGUF": header(arch="qwen35", layers=40, full_attention_interval=4),
        },
    )
    deep = service.repo_detail("a/Deep-GGUF")
    hybrid = service.repo_detail("a/Hybrid-GGUF")
    assert _needed(deep) > _needed(hybrid)
    assert deep["quants"][0]["fit"] == "too_big"
    assert hybrid["quants"][0]["fit"] in ("ok", "tight")


def test_repo_detail_grades_at_the_context_an_install_writes(monkeypatch):
    """A model trained for 8K is installed at 8K, so it needs less than the
    same layers installed at the 32K cap."""
    meta = header(layers=40)
    _stub_repo(monkeypatch, {"a/Short-GGUF": meta}, ctx=8192)
    short = _needed(service.repo_detail("a/Short-GGUF"))
    _stub_repo(monkeypatch, {"a/Long-GGUF": meta}, ctx=262144)
    long = _needed(service.repo_detail("a/Long-GGUF"))
    assert short < long


def test_repo_detail_keeps_the_flat_allowance_when_the_header_is_unreadable(monkeypatch):
    _stub_repo(monkeypatch, {"a/Opaque-GGUF": None})
    detail = service.repo_detail("a/Opaque-GGUF")
    assert _needed(detail) == compat.needed_bytes(8 * GIB)


def test_a_read_header_is_kept_and_a_failed_read_is_tried_again(monkeypatch):
    reads = _stub_repo(monkeypatch, {"a/Deep-GGUF": header(), "a/Opaque-GGUF": None})
    for _ in range(2):
        service.repo_detail("a/Deep-GGUF")
        service.repo_detail("a/Opaque-GGUF")
    assert reads.count("a/Deep-GGUF") == 1
    assert reads.count("a/Opaque-GGUF") == 2


def test_recommended_reads_a_downloaded_model_from_disk(monkeypatch):
    """A downloaded recommendation is graded from its own weights file, with
    no network read, and a model with a larger cache needs more."""
    from server.infrastructure.paths import models_root
    from server.local import registry

    entries = {
        "local_deep": header(layers=40),
        "local_hybrid": header(arch="qwen35", layers=40, full_attention_interval=4),
    }
    catalog = {}
    for key, meta in entries.items():
        catalog[key] = {
            "id": f"local:{key}",
            "label": key,
            "repo_id": f"a/{key}",
            "filename": f"{key}.gguf",
            "dir": key,
            "ctx": CTX,
            "size_bytes": 8 * GIB,
        }
        os.makedirs(os.path.join(models_root(), key), exist_ok=True)
        with open(os.path.join(models_root(), key, f"{key}.gguf"), "wb") as f:
            f.write(gguf_bytes(meta))
    monkeypatch.setattr(registry, "RECOMMENDED_LOCAL_MODELS", catalog)
    monkeypatch.setattr(service, "_total_memory_bytes", lambda: 18 * GIB)
    monkeypatch.setattr(service, "_reserved_companion_bytes", lambda: 0)

    def no_network(repo_id, filename):
        raise AssertionError("a downloaded model must not be read over the network")

    monkeypatch.setattr(service.hf_client, "gguf_file_metadata", no_network)

    rows = {r["key"]: r for r in service.recommended()}
    assert rows["local_deep"]["downloaded"] and rows["local_hybrid"]["downloaded"]
    assert rows["local_deep"]["needed_bytes"] > rows["local_hybrid"]["needed_bytes"]
    assert rows["local_deep"]["fit"] == "too_big"


def test_recommended_reads_a_model_not_on_disk_from_its_repo(monkeypatch):
    from server.local import registry

    key = next(iter(registry.RECOMMENDED_LOCAL_MODELS))
    entry = registry.RECOMMENDED_LOCAL_MODELS[key]
    asked: list[tuple[str, str]] = []

    def read(repo_id, filename):
        asked.append((repo_id, filename))
        return header(layers=40)

    monkeypatch.setattr(service.hf_client, "gguf_file_metadata", read)
    rows = {r["key"]: r for r in service.recommended()}
    assert (entry["repo_id"], entry["filename"]) in asked
    assert rows[key]["needed_bytes"] > rows[key]["size_bytes"] + kv(header(layers=40), entry["ctx"])


# ── hf_client: the ranged read ───────────────────────────────────────────────
class _FakeFs:
    def __init__(self, data: bytes | None):
        self.data = data
        self.opened: list[str] = []

    def __call__(self):
        return self

    def open(self, path, mode="rb", block_size=None):
        self.opened.append(path)
        if self.data is None:
            raise OSError("offline")
        return io.BytesIO(self.data)


def test_hf_header_read_opens_the_file_in_the_repo(monkeypatch):
    import huggingface_hub

    fs = _FakeFs(gguf_bytes(header(layers=12)))
    monkeypatch.setattr(huggingface_hub, "HfFileSystem", fs)
    meta = real_gguf_file_metadata("org/Model-GGUF", "Model-Q4_K_M.gguf")
    assert fs.opened == ["org/Model-GGUF/Model-Q4_K_M.gguf"]
    assert gguf_kv.kv_shape(meta) == gguf_kv.kv_shape(header(layers=12))


@pytest.mark.parametrize("data", [None, b"<html>not a model</html>"])
def test_hf_header_read_answers_none_on_failure(monkeypatch, data):
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfFileSystem", _FakeFs(data))
    assert real_gguf_file_metadata("org/Model-GGUF", "Model-Q4_K_M.gguf") is None
