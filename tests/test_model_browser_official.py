"""Discover's official lane and the engine-read architecture gate.

The engine readers (server/model_browser/engine_archs.py) run against a
stand-in libllama compiled for the test and a fake mlx_lm package. The Hub
client runs against a fake HfApi. The search runs against fake client
answers. No request leaves the test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.model_browser import engine_archs, hf_client, search
from server.model_browser.engine_archs import llama_archs as real_llama_archs
from server.model_browser.engine_archs import mlx_archs as real_mlx_archs
from server.model_browser.hf_client import SearchHit
from server.model_browser.hf_client import conversions as real_conversions
from server.model_browser.hf_client import official_models as real_official_models

# ── engine_archs: llama.cpp ──────────────────────────────────────────────────

_STUB_LIBLLAMA = r"""
static const char *names[] = {"llama", "qwen35", "gemma4", "(unknown)"};
const char *_Z13llm_arch_name8llm_arch(int arch) {
    return (arch >= 0 && arch < 4) ? names[arch] : "unknown";
}
"""


@pytest.fixture
def stub_llama_server(tmp_path, monkeypatch):
    """A llama-server path whose libllama.dylib names three architectures."""
    cc = shutil.which("cc")
    if not cc:
        pytest.skip("no C compiler to build the stand-in libllama")
    src = tmp_path / "stub.c"
    src.write_text(_STUB_LIBLLAMA)
    lib = tmp_path / "libllama.dylib"
    shared = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared", "-fPIC"]
    subprocess.run([cc, *shared, "-o", str(lib), str(src)], check=True)
    exe = tmp_path / "llama-server"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    from server.local import llama_server

    monkeypatch.setattr(llama_server, "binary_path", lambda: str(exe))
    return exe


def test_llama_archs_names_every_arch_the_library_knows(stub_llama_server):
    """The probe names enum values until "(unknown)", so it lists exactly the
    library's architectures, the end marker excluded."""
    assert real_llama_archs() == frozenset({"llama", "qwen35", "gemma4"})


def test_llama_archs_are_empty_without_a_binary_or_library(tmp_path, monkeypatch):
    from server.local import llama_server

    monkeypatch.setattr(llama_server, "binary_path", lambda: None)
    assert real_llama_archs() == frozenset()
    exe = tmp_path / "llama-server"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(llama_server, "binary_path", lambda: str(exe))
    assert real_llama_archs() == frozenset()  # no libllama.dylib beside it


# ── engine_archs: mlx-lm ─────────────────────────────────────────────────────


def test_mlx_archs_are_the_model_modules_plus_remapped_names(tmp_path, monkeypatch):
    pkg = tmp_path / "mlx_lm"
    (pkg / "models").mkdir(parents=True)
    for module in ("llama.py", "qwen3_5.py", "deepseek_v41.py", "_private.py", "notes.txt"):
        (pkg / "models" / module).write_text("")
    (pkg / "utils.py").write_text(
        'MODEL_REMAPPING = {"mistral": "llama", "deepseek_v41_text": "deepseek_v41", '
        '"ghost": "not_installed"}\n'
    )
    spec = SimpleNamespace(submodule_search_locations=[str(pkg)])
    monkeypatch.setattr(engine_archs.importlib.util, "find_spec", lambda name: spec)
    assert real_mlx_archs() == frozenset(
        {"llama", "qwen3_5", "deepseek_v41", "mistral", "deepseek_v41_text"}
    )


def test_mlx_archs_are_empty_without_mlx_lm(monkeypatch):
    monkeypatch.setattr(engine_archs.importlib.util, "find_spec", lambda name: None)
    assert real_mlx_archs() == frozenset()


# ── hf_client ────────────────────────────────────────────────────────────────


class _FakeApi:
    def __init__(self, models):
        self.models = models
        self.calls: list[dict] = []

    def list_models(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.models)


def _model(repo_id, **fields):
    return SimpleNamespace(id=repo_id, downloads=1, likes=0, trending_score=0, **fields)


def test_search_sends_no_pipeline_filter_and_reads_the_base_model(monkeypatch):
    api = _FakeApi(
        [
            _model(
                "unsloth/Qwen3.8-27B-GGUF",
                gguf={"architecture": "qwen35", "context_length": 262144},
                pipeline_tag=None,
                base_models={"relation": "quantized", "models": [{"id": "Qwen/Qwen3.8-27B"}]},
            ),
            _model(
                "someone/Qwen3.8-Tuned-GGUF",
                gguf={"architecture": "qwen35"},
                pipeline_tag="image-text-to-text",
                base_models={"relation": "finetune", "models": [{"id": "Qwen/Qwen3.8-27B"}]},
            ),
        ]
    )
    monkeypatch.setattr(hf_client, "_api", lambda: api)
    hits = hf_client.search("qwen3.8", None, 10, "gguf")
    assert "pipeline_tag" not in api.calls[0]
    assert [h.base_model for h in hits] == ["Qwen/Qwen3.8-27B", None]  # a fine-tune is not a build
    assert hits[1].pipeline_tag == "image-text-to-text"


def test_conversions_ask_for_quantized_builds_of_one_base(monkeypatch):
    api = _FakeApi([_model("mlx-community/X-4bit", config={"model_type": "qwen3"})])
    monkeypatch.setattr(hf_client, "_api", lambda: api)
    (hit,) = real_conversions("Qwen/X", "mlx", 5)
    assert api.calls[0]["filter"] == ["mlx", "base_model:quantized:Qwen/X"]
    assert hit.arch == "qwen3"


def test_official_models_tell_builds_from_base_models(monkeypatch):
    api = _FakeApi(
        [
            _model(
                "Qwen/Qwen3.8-27B",
                pipeline_tag="image-text-to-text",
                tags=[],
                library_name="transformers",
            ),
            _model(
                "Qwen/Qwen3-8B-GGUF",
                pipeline_tag="text-generation",
                tags=["gguf"],
                library_name=None,
            ),
            _model(
                "Qwen/Qwen3-14B-MLX-8bit",
                pipeline_tag="text-generation",
                tags=[],
                library_name="mlx",
            ),
        ]
    )
    monkeypatch.setattr(hf_client, "_api", lambda: api)
    assert [h.is_build for h in real_official_models("qwen", 10)] == [False, True, True]
    assert api.calls[0]["sort"] == "downloads"
    real_official_models(None, 10)
    assert api.calls[1]["sort"] == "trendingScore"


# ── search: the official lane ────────────────────────────────────────────────


def _hit(repo_id, downloads, arch, base_model=None, pipeline_tag=None, is_build=False):
    return SearchHit(
        repo_id, downloads, 0, 0, arch, None, False, pipeline_tag, base_model, is_build
    )


def _client(monkeypatch, *, shards=(), bases=(), builds=None):
    """Fake client answers: ``shards`` for every format search (GGUF lane),
    ``bases`` for the official lane, ``builds[(base, fmt)]`` for conversions."""
    builds = builds or {}
    monkeypatch.setattr(
        search.hf_client,
        "search",
        lambda query, author, limit, fmt="gguf": [
            h
            for h in shards
            if fmt == "gguf" and (author is None or h.repo_id.startswith(f"{author}/"))
        ],
    )
    monkeypatch.setattr(search.hf_client, "official_models", lambda query, limit: list(bases))
    monkeypatch.setattr(
        search.hf_client,
        "conversions",
        lambda base_model, fmt, limit: list(builds.get((base_model, fmt), [])),
    )


def test_an_official_build_outranks_a_more_downloaded_community_build(monkeypatch):
    _client(
        monkeypatch,
        shards=[
            _hit("unsloth/Qwen3.8-27B-Uncensored-GGUF", 9_000_000, "qwen35"),
            _hit("unsloth/Qwen3.8-27B-GGUF", 100, "qwen35", base_model="Qwen/Qwen3.8-27B"),
        ],
    )
    rows = search.search("qwen3.8")
    assert [r["repo_id"] for r in rows] == [
        "unsloth/Qwen3.8-27B-GGUF",
        "unsloth/Qwen3.8-27B-Uncensored-GGUF",
    ]
    assert rows[0]["official"] and rows[0]["base_model"] == "Qwen/Qwen3.8-27B"
    assert not rows[1]["official"]


def test_the_official_lane_finds_builds_of_the_makers_model(monkeypatch):
    base = _hit("deepseek-ai/DeepSeek-V9-Flash", 5_000_000, None, pipeline_tag="text-generation")
    trusted = _hit("unsloth/DeepSeek-V9-Flash-GGUF", 10, "deepseek4")
    community = _hit("someone/DeepSeek-V9-Flash-GGUF", 900, "deepseek4")
    _client(
        monkeypatch,
        bases=[base],
        builds={(base.repo_id, "gguf"): [community, trusted]},
    )
    default = [r["repo_id"] for r in search.search("deepseek flash")]
    assert default == ["unsloth/DeepSeek-V9-Flash-GGUF"]  # trusted publishers only
    wide = search.search("deepseek flash", all_of_hf=True)
    assert [r["repo_id"] for r in wide] == [
        "unsloth/DeepSeek-V9-Flash-GGUF",  # official, trusted publisher
        "someone/DeepSeek-V9-Flash-GGUF",  # official, another publisher
    ]
    assert all(r["official"] and r["base_model"] == base.repo_id for r in wide)


def test_an_official_model_the_engines_cannot_run_gets_one_row_that_says_why(monkeypatch):
    unknown = _hit(
        "deepseek-ai/DeepSeek-V9.1-Flash", 7_000_000, None, pipeline_tag="text-generation"
    )
    none_yet = _hit(
        "deepseek-ai/DeepSeek-V9.2-Flash", 6_000_000, None, pipeline_tag="text-generation"
    )
    community_only = _hit("deepseek-ai/DeepSeek-V9.3-Flash", 5_000_000, None)
    _client(
        monkeypatch,
        shards=[_hit("unsloth/Flash-Helper-GGUF", 1, "llama")],
        bases=[unknown, none_yet, community_only],
        builds={
            (unknown.repo_id, "gguf"): [_hit("unsloth/DeepSeek-V9.1-Flash-GGUF", 50, "deepseek41")],
            (community_only.repo_id, "gguf"): [
                _hit("someone/DeepSeek-V9.3-Flash-GGUF", 9, "deepseek4")
            ],
        },
    )
    rows = {r["repo_id"]: r for r in search.search("deepseek flash")}
    assert "unsloth/DeepSeek-V9.1-Flash-GGUF" not in rows  # the engine does not know deepseek41
    reasons = {
        repo: rows[repo]["unsupported_reason"]
        for repo in (unknown.repo_id, none_yet.repo_id, community_only.repo_id)
    }
    assert "deepseek41" in reasons[unknown.repo_id]
    assert "no GGUF or MLX build" in reasons[none_yet.repo_id]
    assert "All of Hugging Face" in reasons[community_only.repo_id]
    for repo in reasons:
        assert rows[repo]["supported"] is False
        assert rows[repo]["official"] is True
        assert rows[repo]["format"] is None
        assert rows[repo]["install_state"] is None
    # In all of Hugging Face the community build is offered, so its model needs
    # no explanation row.
    wide = {r["repo_id"] for r in search.search("deepseek flash", all_of_hf=True)}
    assert "someone/DeepSeek-V9.3-Flash-GGUF" in wide
    assert community_only.repo_id not in wide


def test_a_makers_own_build_is_not_taken_for_a_base_model(monkeypatch):
    own_build = _hit("Qwen/Qwen9-8B-GGUF", 100, None, pipeline_tag="text-generation", is_build=True)
    _client(monkeypatch, bases=[own_build])
    assert search.search("qwen9") == []  # no "no build" row for a repo that is a build


def test_non_chat_models_never_show(monkeypatch):
    _client(
        monkeypatch,
        shards=[
            _hit(
                "unsloth/Qwen3-Embedding-8B-GGUF", 900, "qwen3", pipeline_tag="feature-extraction"
            ),
            _hit("unsloth/Qwen3-VL-mmproj-GGUF", 800, "clip"),
            _hit("unsloth/Qwen3-8B-GGUF", 700, "qwen3", pipeline_tag=None),
            _hit("unsloth/Qwen3-VL-8B-GGUF", 600, "qwen3", pipeline_tag="image-text-to-text"),
        ],
    )
    assert [r["repo_id"] for r in search.search("qwen3")] == [
        "unsloth/Qwen3-8B-GGUF",
        "unsloth/Qwen3-VL-8B-GGUF",
    ]


def test_an_explicit_author_skips_the_official_lane(monkeypatch):
    asked: list = []
    monkeypatch.setattr(search.hf_client, "search", lambda q, a, limit, fmt="gguf": [])
    monkeypatch.setattr(
        search.hf_client, "official_models", lambda query, limit: asked.append(query) or []
    )
    search.search("qwen", author="unsloth")
    assert asked == []


def test_the_vendor_binary_path_is_inside_the_repo():
    """setup.sh's vendor copy lives under the checkout, never a system dir."""
    from server.local import llama_server

    repo = Path(__file__).resolve().parents[1]
    assert os.path.commonpath([llama_server._VENDOR_BINARY, repo]) == str(repo)
