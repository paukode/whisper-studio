"""MLX chat engine: registry shape, engine dispatch, downloads, and the
Discover MLX lane.

The contracts worth pinning:

  - An ``engine: "mlx"`` entry needs no ``filename`` (whole-repo snapshot);
    a GGUF entry without one is still dropped.
  - serving.* is the ONE dispatch point: mlx keys go to mlx_server, everything
    else to llama_server, and starting one engine stops the other (one local
    model resident at a time is a CROSS-engine invariant).
  - The wire model name for mlx is the ``default_model`` sentinel — sending the
    registry key would make mlx_lm try to load the key as a repo.
  - mlx downloads are complete only when the marker file exists; config.json
    landing early must not read as installed.
  - The Discover MLX lane gates on transformers-config ``model_type``, installs
    whole-repo entries, and never requires a filename.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from server.chat.engine.local import LocalAdapter
from server.local import mlx_server, runtime, serving
from server.local import registry as R
from server.model_browser import compat, install, service

MLX_ENTRY = {
    "id": "local:qwen3-4b-mlx",
    "label": "Qwen3 4B (Local MLX)",
    "engine": "mlx",
    "repo_id": "mlx-community/Qwen3-4B-4bit",
    "dir": "mlx-community__Qwen3-4B-4bit",
    "supports_thinking": True,
    "supports_tools": True,
}
GGUF_ENTRY = {
    "id": "local:qwen-gguf",
    "label": "Qwen GGUF (Local)",
    "repo_id": "unsloth/Qwen3-8B-GGUF",
    "filename": "Qwen3-8B-Q4_K_M.gguf",
    "dir": "qwen3-8b",
}


def _seed_registry(monkeypatch, entries):
    monkeypatch.setattr(R, "_config_local_models", lambda: dict(entries))
    monkeypatch.setattr(R, "_recommended_downloaded_on_disk", lambda entry: False)


# ------------------------------------------------------------------ registry


def test_mlx_entry_is_usable_without_filename(monkeypatch):
    _seed_registry(monkeypatch, {"local_mlx": dict(MLX_ENTRY)})
    models = R.local_models()
    assert "local_mlx" in models
    assert models["local_mlx"]["engine"] == "mlx"
    assert models["local_mlx"]["ctx"] == 32768  # default applied


def test_gguf_entry_without_filename_is_still_dropped(monkeypatch):
    broken = {k: v for k, v in GGUF_ENTRY.items() if k != "filename"}
    _seed_registry(monkeypatch, {"local_broken": broken})
    assert "local_broken" not in R.local_models()


# ------------------------------------------------------------------- serving


def test_engine_dispatch_helpers(monkeypatch):
    _seed_registry(monkeypatch, {"local_mlx": dict(MLX_ENTRY), "local_gguf": dict(GGUF_ENTRY)})
    assert serving.engine_of("local_mlx") == "mlx"
    assert serving.engine_of("local_gguf") == "gguf"
    assert serving.engine_of("unknown") == "gguf"
    assert serving.wire_model("local_mlx") == "default_model"
    assert serving.wire_model("local_gguf") == "local_gguf"
    assert serving.supports_tool_choice("local_gguf") is True
    assert serving.supports_tool_choice("local_mlx") is False


def test_ensure_serving_validates_and_downloads_before_evicting_the_other_engine(
    monkeypatch,
):
    """The other engine's resident model is evicted only AFTER the target
    engine is available and the weights are on disk — a doomed or slow request
    must never leave the machine with nothing resident."""
    _seed_registry(monkeypatch, {"local_mlx": dict(MLX_ENTRY), "local_gguf": dict(GGUF_ENTRY)})
    calls: list[str] = []
    monkeypatch.setattr(
        serving.mlx_server, "ensure_available", lambda: calls.append("mlx.available")
    )
    monkeypatch.setattr(runtime, "ensure_downloaded", lambda key: calls.append(f"download:{key}"))
    monkeypatch.setattr(serving.llama_server, "resident_key", lambda: "local_gguf")
    monkeypatch.setattr(serving.llama_server, "is_running", lambda: True)
    monkeypatch.setattr(serving.llama_server, "stop", lambda: calls.append("llama.stop"))
    monkeypatch.setattr(serving.mlx_server, "resident_key", lambda: None)
    monkeypatch.setattr(
        serving.mlx_server,
        "ensure_serving",
        lambda key, n_ctx=None: calls.append(f"mlx.serve:{key}") or "http://mlx",
    )
    assert serving.ensure_serving("local_mlx") == "http://mlx"
    assert calls == ["mlx.available", "download:local_mlx", "llama.stop", "mlx.serve:local_mlx"]

    calls.clear()
    monkeypatch.setattr(
        serving.llama_server, "ensure_available", lambda: calls.append("llama.available")
    )
    monkeypatch.setattr(serving.llama_server, "resident_key", lambda: None)
    monkeypatch.setattr(serving.mlx_server, "is_running", lambda: True)
    monkeypatch.setattr(serving.mlx_server, "stop", lambda: calls.append("mlx.stop"))
    monkeypatch.setattr(
        serving.llama_server,
        "ensure_serving",
        lambda key, n_ctx=None: calls.append(f"llama.serve:{key}") or "http://llama",
    )
    assert serving.ensure_serving("local_gguf") == "http://llama"
    assert calls == [
        "llama.available",
        "download:local_gguf",
        "mlx.stop",
        "llama.serve:local_gguf",
    ]


def test_ensure_serving_refuses_before_evicting_on_doomed_requests(monkeypatch):
    """mlx-lm missing or an unknown key must fail WITHOUT stopping whatever is
    currently resident (the request could never have succeeded)."""
    _seed_registry(monkeypatch, {"local_mlx": dict(MLX_ENTRY), "local_gguf": dict(GGUF_ENTRY)})
    stops: list[str] = []
    monkeypatch.setattr(serving.llama_server, "stop", lambda: stops.append("llama"))
    monkeypatch.setattr(serving.mlx_server, "stop", lambda: stops.append("mlx"))
    monkeypatch.setattr(serving.llama_server, "resident_key", lambda: "local_gguf")
    monkeypatch.setattr(serving.mlx_server, "resident_key", lambda: None)
    monkeypatch.setattr(serving.llama_server, "is_running", lambda: True)

    def unavailable():
        raise mlx_server.MlxServerUnavailable("mlx-lm is not installed")

    monkeypatch.setattr(serving.mlx_server, "ensure_available", unavailable)
    with pytest.raises(mlx_server.MlxServerUnavailable):
        serving.ensure_serving("local_mlx")
    assert stops == []

    monkeypatch.setattr(serving.llama_server, "ensure_available", lambda: None)
    with pytest.raises(RuntimeError, match="Unknown local model"):
        serving.ensure_serving("removed_key")
    assert stops == []


def test_ensure_serving_fast_path_skips_the_transition_lock(monkeypatch):
    """A turn on the already-resident model must not queue behind another
    model's download holding the transition lock."""
    _seed_registry(monkeypatch, {"local_mlx": dict(MLX_ENTRY)})
    monkeypatch.setattr(serving.mlx_server, "resident_key", lambda: "local_mlx")
    monkeypatch.setattr(serving.mlx_server, "base_url", lambda: "http://mlx-fast")
    monkeypatch.setattr(serving.mlx_server, "resident_n_ctx", lambda: 32768)
    with serving._transition_lock:  # a download in flight elsewhere
        assert serving.ensure_serving("local_mlx", 32768) == "http://mlx-fast"


def test_resident_key_covers_both_engines(monkeypatch):
    monkeypatch.setattr(serving.llama_server, "resident_key", lambda: None)
    monkeypatch.setattr(serving.mlx_server, "resident_key", lambda: "local_mlx")
    assert serving.resident_key() == "local_mlx"
    monkeypatch.setattr(serving.llama_server, "resident_key", lambda: "local_gguf")
    assert serving.resident_key() == "local_gguf"


# ------------------------------------------------------------------- runtime


def test_mlx_download_marker_semantics(monkeypatch, tmp_path):
    _seed_registry(monkeypatch, {"local_mlx": dict(MLX_ENTRY)})
    monkeypatch.setattr(runtime, "MODELS_DIR", str(tmp_path))

    target = tmp_path / MLX_ENTRY["dir"]
    assert runtime.is_mlx_model("local_mlx")
    assert runtime.model_path("local_mlx") == str(target)
    assert not runtime.is_downloaded("local_mlx")

    def fake_snapshot(repo_id, local_dir):
        # config.json landing first must NOT count as installed.
        os.makedirs(local_dir, exist_ok=True)
        (tmp_path / MLX_ENTRY["dir"] / "config.json").write_text("{}")
        assert not runtime.is_downloaded("local_mlx")
        (tmp_path / MLX_ENTRY["dir"] / "model.safetensors").write_text("w")

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    path = runtime.ensure_downloaded("local_mlx")
    assert path == str(target)
    assert runtime.is_downloaded("local_mlx")
    assert (target / runtime.MLX_COMPLETE_MARKER).exists()


def test_gguf_model_path_still_points_at_the_file(monkeypatch, tmp_path):
    _seed_registry(monkeypatch, {"local_gguf": dict(GGUF_ENTRY)})
    monkeypatch.setattr(runtime, "MODELS_DIR", str(tmp_path))
    assert runtime.model_path("local_gguf") == str(
        tmp_path / GGUF_ENTRY["dir"] / GGUF_ENTRY["filename"]
    )
    assert not runtime.is_mlx_model("local_gguf")


# ------------------------------------------------------------------- catalog


def test_catalog_mlx_entry_uses_marker_sentinel_and_whole_repo_size(monkeypatch):
    _seed_registry(monkeypatch, {"local_mlx": dict(MLX_ENTRY), "local_gguf": dict(GGUF_ENTRY)})
    from server.models_manager import catalog

    by_key = {e.key: e for e in catalog._local_chat_entries()}
    mlx = by_key["local_mlx"]
    assert mlx.sentinel_rel == runtime.MLX_COMPLETE_MARKER
    assert mlx.gguf_filename is None  # size estimate sums the whole repo
    gguf = by_key["local_gguf"]
    assert gguf.sentinel_rel == GGUF_ENTRY["filename"]
    assert gguf.gguf_filename == GGUF_ENTRY["filename"]


# ---------------------------------------------------------------- mlx_server


class _FakeProc:
    def __init__(self):
        self.pid = 4242
        self.terminated = False

    def poll(self):
        return 1 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def test_mlx_server_spawns_the_module_server_and_warms_up(monkeypatch, tmp_path):
    _seed_registry(monkeypatch, {"local_mlx": dict(MLX_ENTRY)})
    monkeypatch.setattr(runtime, "MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(mlx_server, "ensure_available", lambda: None)
    model_dir = str(tmp_path / MLX_ENTRY["dir"])
    monkeypatch.setattr(runtime, "ensure_downloaded", lambda key: model_dir)

    spawned: dict = {}

    def fake_popen(cmd, **kw):
        spawned["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(mlx_server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mlx_server, "_wait_healthy", lambda port, proc, deadline: True)
    warmed: dict = {}

    def fake_warmup(port, deadline):
        warmed["port"] = port
        return None

    monkeypatch.setattr(mlx_server, "_warmup", fake_warmup)

    try:
        url = mlx_server.ensure_serving("local_mlx")
        cmd = spawned["cmd"]
        assert cmd[1:5] == ["-m", "mlx_lm", "server", "--model"]
        assert cmd[5] == model_dir
        assert mlx_server.resident_key() == "local_mlx"
        assert url == mlx_server.base_url()
        assert warmed["port"] is not None
        # Re-request: same process reused, no second spawn.
        spawned.clear()
        assert mlx_server.ensure_serving("local_mlx") == url
        assert "cmd" not in spawned
    finally:
        mlx_server.stop()
    assert mlx_server.resident_key() is None


def test_warmup_message_stays_longer_than_mlx_lm_segmentation_window():
    """mlx_lm 0.31.3 segments prompts with rfind_think_start(start=len-11) and
    IndexErrors on prompts shorter than 11 tokens (observed as a 404 "list
    index out of range" loading DeepSeek-R1, whose compact template rendered a
    one-word warmup to 4 tokens). Words tokenize to at least one token each,
    so this word count keeps every template safely past the window."""
    assert len(mlx_server.WARMUP_MESSAGE.split()) >= 15


def test_mlx_server_warmup_failure_stops_and_raises(monkeypatch, tmp_path):
    _seed_registry(monkeypatch, {"local_mlx": dict(MLX_ENTRY)})
    monkeypatch.setattr(runtime, "MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(mlx_server, "ensure_available", lambda: None)
    monkeypatch.setattr(runtime, "ensure_downloaded", lambda key: str(tmp_path / "d"))
    monkeypatch.setattr(mlx_server.subprocess, "Popen", lambda cmd, **kw: _FakeProc())
    monkeypatch.setattr(mlx_server, "_wait_healthy", lambda port, proc, deadline: True)
    monkeypatch.setattr(mlx_server, "_warmup", lambda port, deadline: "boom")
    with pytest.raises(RuntimeError, match="could not load"):
        mlx_server.ensure_serving("local_mlx")
    assert not mlx_server.is_running()


# ------------------------------------------------------------------- adapter


class _FakeRounds:
    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.payloads: list[dict] = []

    def __call__(self, base_url, payload):
        self.payloads.append(payload)
        pieces, calls = self.rounds.pop(0)

        async def gen():
            for kind, piece in pieces:
                yield (kind, piece)
            yield ("done", {"calls": calls, "finish_reason": "stop", "usage": {}})

        return gen()


def test_adapter_sends_the_wire_model_not_the_key(monkeypatch):
    from server.local import server_stream as SS

    fake = _FakeRounds(([("text", "hi")], []))
    monkeypatch.setattr(SS, "_stream_round", fake)
    adapter = LocalAdapter(
        model_key="local_mlx",
        base_url="http://x",
        system_prompt="",
        thinking=False,
        tools_enabled=False,
        wire_model="default_model",
        supports_tool_choice=False,
    )

    async def go():
        return [
            e
            async for e in adapter.stream_round(
                [{"role": "user", "content": "q"}], [], None, 1, False
            )
        ]

    asyncio.run(go())
    assert fake.payloads[0]["model"] == "default_model"


def test_adapter_omits_tools_on_last_round_without_tool_choice(monkeypatch):
    from server.local import server_stream as SS

    fake = _FakeRounds(([("text", "final")], []))
    monkeypatch.setattr(SS, "_stream_round", fake)
    adapter = LocalAdapter(
        model_key="local_mlx",
        base_url="http://x",
        system_prompt="",
        thinking=False,
        tools_enabled=True,
        wire_model="default_model",
        supports_tool_choice=False,
    )
    tools = [{"name": "t", "description": "", "input_schema": {}}]

    async def go():
        return [
            e
            async for e in adapter.stream_round(
                [{"role": "user", "content": "q"}], tools, None, 49, True
            )
        ]

    asyncio.run(go())
    # tool_choice:"none" would be ignored by mlx_lm, so the tools must be gone.
    assert "tools" not in fake.payloads[0]
    assert "tool_choice" not in fake.payloads[0]


# ------------------------------------------------------- stream (mlx dialect)


def test_stream_round_reads_mlx_reasoning_field(monkeypatch):
    """mlx_lm puts reasoning on ``delta.reasoning`` (llama-server uses
    ``reasoning_content``); both must land on the thinking channel."""
    from server.local import server_stream as SS

    frames = [
        {"choices": [{"delta": {"reasoning": "let me think"}}]},
        {"choices": [{"delta": {"content": "answer"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    lines = [f"data: {json.dumps(f)}" for f in frames] + ["data: [DONE]"]

    class _Resp:
        status_code = 200

        async def aiter_lines(self):
            for line in lines:
                yield line

        async def aread(self):
            return b""

    class _StreamCM:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *a, **kw):
            return _StreamCM()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def go():
        return [item async for item in SS._stream_round("http://x", {"model": "m"})]

    events = asyncio.run(go())
    assert ("thinking", "let me think") in events
    assert ("text", "answer") in events


# ------------------------------------------------------------- Discover lane


def test_parse_mlx_quant_variants():
    assert compat.parse_mlx_quant("mlx-community/Qwen3-4B-4bit") == "4bit"
    assert compat.parse_mlx_quant("mlx-community/Llama-3.3-70B-8bit") == "8bit"
    assert compat.parse_mlx_quant("mlx-community/gemma-4-12b-it-bf16") == "bf16"
    assert compat.parse_mlx_quant("mlx-community/Qwen3-4B-4bit-DWQ") == "4bit-dwq"
    assert compat.parse_mlx_quant("mlx-community/NoTag") is None


def test_mlx_arch_gate():
    assert compat.mlx_arch_supported("qwen3")
    assert compat.mlx_arch_supported("gemma4")
    assert not compat.mlx_arch_supported("t5")
    assert not compat.mlx_arch_supported(None)


def test_mlx_search_uses_mlx_authors_and_arch_gate(monkeypatch):
    from server.model_browser.hf_client import SearchHit

    seen: list[tuple] = []

    def fake_search(query, author, limit, fmt="gguf"):
        seen.append((author, fmt))
        if fmt != "mlx":
            return []
        return [
            SearchHit(
                repo_id=f"{author}/Qwen3-4B-4bit",
                downloads=10,
                likes=1,
                trending_score=5,
                arch="qwen3",
                context_length=None,
                gated=False,
            ),
            SearchHit(
                repo_id=f"{author}/T5-thing",
                downloads=99,
                likes=1,
                trending_score=9,
                arch="t5",
                context_length=None,
                gated=False,
            ),
        ]

    monkeypatch.setattr(service.hf_client, "search", fake_search)
    rows = service.search(query="qwen")
    # The single merged search fans out over BOTH lanes' trusted authors.
    assert {a for a, f in seen if f == "mlx"} == set(compat.MLX_TRUSTED_AUTHORS)
    assert {a for a, f in seen if f == "gguf"} == set(compat.TRUSTED_AUTHORS)
    assert rows and all(r["format"] == "mlx" for r in rows)
    assert all("T5" not in r["repo_id"] for r in rows), "unsupported arch must drop"


def test_mlx_repo_detail_is_one_whole_repo_option(monkeypatch):
    monkeypatch.setattr(service.hf_client, "repo_mlx_meta", lambda repo_id: ("qwen3", True))
    monkeypatch.setattr(
        service.hf_client,
        "repo_files",
        lambda repo_id: [("config.json", 100), ("model.safetensors", 4_000_000_000)],
    )
    monkeypatch.setattr(service.hf_client, "repo_is_gated", lambda repo_id: False)
    detail = service.repo_detail("mlx-community/Qwen3-4B-4bit", fmt="mlx")
    assert detail["format"] == "mlx"
    assert detail["supported"] is True
    (quant,) = detail["quants"]
    assert quant["quant"] == "4bit"
    assert quant["filename"] == ""
    assert quant["size_bytes"] == 4_000_000_100
    assert quant["recommended"] is True


def test_mlx_install_refuses_unsupported_model_type(monkeypatch):
    monkeypatch.setattr(service.hf_client, "repo_mlx_meta", lambda repo_id: ("t5", True))
    with pytest.raises(service.InstallError, match="model_type"):
        service.install_model("mlx-community/T5-4bit", "", fmt="mlx")


def test_mlx_install_refuses_repos_without_mlx_weights(monkeypatch):
    """A GGUF repo carries a config.json too — the arch gate alone would accept
    it and snapshot gigabytes mlx_lm cannot load. The mlx tag is the proof of
    format."""
    monkeypatch.setattr(service.hf_client, "repo_mlx_meta", lambda repo_id: ("qwen3", False))
    with pytest.raises(service.InstallError, match="MLX-format weights"):
        service.install_model("unsloth/Qwen3-8B-GGUF", "", fmt="mlx")


def test_build_mlx_entry_shape():
    key, entry = install.build_mlx_entry(
        repo_id="mlx-community/Qwen3-30B-4bit", arch="qwen3", label=None, n_ctx=None
    )
    assert key == "local_mlx_community_qwen3_30b_4bit__4bit"
    assert entry["engine"] == "mlx"
    assert entry["is_local"] is True
    assert "filename" not in entry
    # Namespaced away from the GGUF lane's dir for the same repo: MLX deletes
    # remove the whole directory, so the dirs must never collide.
    assert entry["dir"] == "mlx-community__Qwen3-30B-4bit__mlx"
    assert entry["id"].startswith("local:")
    assert entry["ctx"] == 32768
    assert entry["supports_tools"] is True  # 30B clears the agentic gate
    assert entry["supports_thinking"] is True  # qwen3 family


class _CatEntry:
    def __init__(self, key, group):
        self.key = key
        self.group = group


def _seed_install_states(monkeypatch, states: dict[str, str]):
    """Fake the models-manager view: catalog rows for the given keys, each in
    the given state ("installed" / "downloading" / "queued" / "absent")."""
    from server.models_manager import catalog as cat
    from server.models_manager import manager as mgr

    monkeypatch.setattr(
        cat, "entries", lambda: [_CatEntry(k, cat.GROUP_LOCAL_CHAT) for k in states]
    )
    monkeypatch.setattr(mgr, "state_of", lambda e: (states[e.key], None))


def test_search_rows_carry_install_state(monkeypatch):
    """A repo the user already has (or is downloading) must say so in the
    search results, so the same multi-GB download is never queued twice."""
    from server.model_browser.hf_client import SearchHit

    _seed_registry(monkeypatch, {"local_mlx": dict(MLX_ENTRY), "local_gguf": dict(GGUF_ENTRY)})
    _seed_install_states(monkeypatch, {"local_mlx": "downloading", "local_gguf": "installed"})

    def fake_search(query, author, limit, fmt="gguf"):
        if fmt == "mlx":
            return [SearchHit(MLX_ENTRY["repo_id"], 10, 1, 5, "qwen3", None, False)]
        return [
            SearchHit(GGUF_ENTRY["repo_id"], 10, 1, 5, "llama", 4096, False),
            SearchHit("unsloth/Fresh-GGUF", 3, 1, 2, "llama", 4096, False),
        ]

    monkeypatch.setattr(service.hf_client, "search", fake_search)

    rows = {r["repo_id"]: r for r in service.search(query="q", all_of_hf=True)}
    mlx_row = rows[MLX_ENTRY["repo_id"]]
    assert mlx_row["format"] == "mlx"
    assert mlx_row["install_state"] == "downloading"

    installed = rows[GGUF_ENTRY["repo_id"]]
    assert installed["format"] == "gguf"
    assert installed["install_state"] == "installed"
    # Per-quant filenames, so the picker can mark the exact quant while other
    # quants of the same repo stay downloadable.
    assert installed["installed_filenames"] == [GGUF_ENTRY["filename"]]
    assert rows["unsloth/Fresh-GGUF"]["install_state"] is None


def test_recommended_rows_carry_a_live_downloading_flag(monkeypatch):
    from server.local import registry as reg

    monkeypatch.setattr(reg, "_recommended_downloaded_on_disk", lambda entry: False)
    monkeypatch.setattr(reg, "_config_local_models", lambda: {})
    some_key = next(iter(reg.RECOMMENDED_LOCAL_MODELS))
    _seed_install_states(monkeypatch, {some_key: "downloading"})

    rows = {r["key"]: r for r in service.recommended()}
    assert rows[some_key]["downloading"] is True
    others = [r for k, r in rows.items() if k != some_key]
    assert all(r["downloading"] is False for r in others)


def test_build_mlx_entry_uses_the_real_window_from_config_json():
    _, small = install.build_mlx_entry(
        repo_id="mlx-community/Tiny-8k-4bit", arch="llama", label=None, n_ctx=None, header_ctx=8192
    )
    assert small["ctx"] == 8192  # an 8K model must not claim 32K
    _, big = install.build_mlx_entry(
        repo_id="mlx-community/Big-4bit", arch="llama", label=None, n_ctx=None, header_ctx=131072
    )
    assert big["ctx"] == 32768  # capped like a GGUF header ctx


def test_engine_field_survives_the_real_config_normalizer(monkeypatch, tmp_path):
    """Regression: _normalize_chat_models used to drop `engine`, so every real
    MLX entry was rejected for a missing filename. Route an entry through the
    REAL normalizer instead of stubbing _config_local_models."""
    from server.infrastructure.config import _normalize_chat_models

    ids, meta = _normalize_chat_models({"local_mlx": {**MLX_ENTRY, "is_local": True}})
    assert meta["local_mlx"]["engine"] == "mlx"

    monkeypatch.setattr("server.infrastructure.config.unfiltered_chat_models", lambda: (ids, meta))
    monkeypatch.setattr(R, "_recommended_downloaded_on_disk", lambda entry: False)
    models = R.local_models()
    assert "local_mlx" in models, "normalizer must carry engine or mlx entries vanish"
    assert models["local_mlx"]["engine"] == "mlx"


def test_stream_round_raises_when_the_stream_dies_mid_generation(monkeypatch):
    """mlx_lm's HTTP/1.0 body is close-delimited: a mid-generation death reads
    as clean EOF. Without a finish_reason/[DONE] the round must ERROR, not
    commit a truncated answer."""
    from server.local import server_stream as SS

    frames = [{"choices": [{"delta": {"content": "half an ans"}}]}]
    lines = [f"data: {json.dumps(f)}" for f in frames]  # no finish_reason, no [DONE]

    class _Resp:
        status_code = 200

        async def aiter_lines(self):
            for line in lines:
                yield line

        async def aread(self):
            return b""

    class _StreamCM:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *a, **kw):
            return _StreamCM()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def go():
        return [item async for item in SS._stream_round("http://x", {"model": "m"})]

    with pytest.raises(RuntimeError, match="mid-generation"):
        asyncio.run(go())
