"""Index LLM passes and Local mode: a folder saved with the cloud Haiku engine
(typed relations, entity descriptions, chunk headers) sends nothing to Bedrock
while the app is in Local mode, and its saved choice is kept for a switch back.
Hybrid and Cloud still run those passes on Haiku."""

from __future__ import annotations

import numpy as np
import pytest

from server.index import embedder, extractor, paths, pipeline, relations, wssettings
from server.index.config import EMBED_DIM

_HAIKU_EVERYWHERE = {
    "typed_relations": {"enabled": True, "engine": "haiku"},
    "entity_descriptions": {"enabled": True, "engine": "haiku"},
    "chunk_context": {"mode": "llm", "engine": "haiku"},
}


@pytest.fixture(autouse=True)
def _tmp_index(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "INDEX_DATA_DIR", str(tmp_path / "index"))
    # Resolved at import time, so it needs its own redirect.
    monkeypatch.setattr(wssettings, "_PENDING_PATH", str(tmp_path / "pending.json"))


@pytest.fixture
def set_mode(monkeypatch):
    from server.infrastructure import config as cfg_mod

    real = cfg_mod.load_config()

    def _set(mode: str) -> None:
        cfg = {**real, "model_mode": mode}
        monkeypatch.setattr(cfg_mod, "load_config", lambda *a, **k: cfg)

    return _set


@pytest.fixture
def bedrock_calls(monkeypatch):
    calls: list[str] = []

    class _Client:
        def invoke_model(self, *, modelId, **_kw):  # noqa: N803 - boto3 kwarg
            calls.append(modelId)
            raise RuntimeError("stub: no network in tests")

    monkeypatch.setattr("server.chat.infra._get_bedrock_client", lambda: _Client())
    return calls


def _stub_models(monkeypatch):
    """No embedder or NER model loads; every capability runs on-device stubs."""
    from server.infrastructure import model_mode

    local = {"embed": "qwen3", "rerank": "qwen3", "ner": "gliner", "index_llm": "local"}
    monkeypatch.setattr(model_mode, "resolve_backend", lambda cap, config=None: local[cap])
    unit = np.ones(EMBED_DIM, np.float32) / np.sqrt(EMBED_DIM)
    monkeypatch.setattr(
        embedder,
        "embed_documents",
        lambda texts: np.stack([unit for _ in texts]) if texts else np.zeros((0, EMBED_DIM)),
    )
    monkeypatch.setattr(
        extractor,
        "extract_entities",
        lambda t, *a, **k: [
            {"name": "Acme", "label": "Company", "score": 0.9},
            {"name": "Bob", "label": "Person", "score": 0.9},
        ],
    )
    monkeypatch.setattr(embedder, "unload", lambda: None)
    monkeypatch.setattr(extractor, "unload", lambda: None)


def _build(tmp_path) -> str:
    (tmp_path / "notes.txt").write_text("Bob works at Acme. Acme hired Bob last year.\n")
    ws = str(tmp_path)
    wssettings.update_settings(ws, _HAIKU_EVERYWHERE)
    pipeline.build(ws)
    return ws


def test_local_mode_build_sends_nothing_to_bedrock(tmp_path, set_mode, bedrock_calls, monkeypatch):
    set_mode("local")
    _stub_models(monkeypatch)
    ws = _build(tmp_path)
    assert bedrock_calls == []
    # The folder keeps what the user saved; only the build skipped it.
    saved = wssettings.get_settings(ws)
    assert all(saved[p]["engine"] == "haiku" for p in _HAIKU_EVERYWHERE)


@pytest.mark.parametrize("mode", ["hybrid", "cloud"])
def test_cloud_modes_still_run_the_haiku_passes(
    tmp_path, set_mode, bedrock_calls, monkeypatch, mode
):
    set_mode(mode)
    _stub_models(monkeypatch)
    _build(tmp_path)
    assert bedrock_calls  # the same saved settings do reach Haiku outside Local mode


def test_settings_for_build_skips_exactly_the_haiku_passes_in_local_mode(tmp_path, set_mode):
    ws = str(tmp_path)
    wssettings.update_settings(
        ws, {**_HAIKU_EVERYWHERE, "entity_descriptions": {"enabled": True, "engine": "local"}}
    )
    set_mode("local")
    built = wssettings.settings_for_build(ws)
    stored = wssettings.get_settings(ws)
    for p in wssettings.LLM_PASSES:
        if stored[p]["engine"] == wssettings.CLOUD_ENGINE:
            assert built[p]["engine"] == wssettings.SKIP_ENGINE
        else:
            assert built[p]["engine"] == stored[p]["engine"]  # on-device picks untouched
    set_mode("hybrid")
    assert wssettings.settings_for_build(ws) == wssettings.get_settings(ws)


def test_direct_haiku_relation_call_is_refused_in_local_mode(set_mode, bedrock_calls):
    set_mode("local")
    out = relations.extract_relations("Bob works at Acme.", ["Bob", "Acme"], "haiku")
    assert out == [] and bedrock_calls == []


def test_a_file_indexed_in_local_mode_gets_its_haiku_passes_back_in_hybrid(
    tmp_path, set_mode, bedrock_calls, monkeypatch
):
    """The index stamps the folder's choice (context_mode=llm), so a Local
    build's filename-only headers and empty relations used to stay: the next
    Hybrid build's size/mtime gate skipped the unchanged file."""
    _stub_models(monkeypatch)
    folder = tmp_path / "ws"
    folder.mkdir()
    (folder / "notes.txt").write_text("Bob works at Acme. Acme hired Bob last year.\n")
    ws = str(folder)
    wssettings.update_settings(
        ws,
        {
            "typed_relations": {"enabled": True, "engine": "haiku"},
            "chunk_context": {"mode": "llm", "engine": "haiku"},
        },
    )
    set_mode("local")
    pipeline.build(ws)
    assert bedrock_calls == []

    set_mode("hybrid")
    assert pipeline.build(ws)["changed_files"] == 1  # the file is redone, not skipped
    assert bedrock_calls  # its chunk header and relations went to Haiku
    calls = len(bedrock_calls)
    # Once redone it owes nothing: the next build skips it as unchanged.
    assert pipeline.build(ws)["changed_files"] == 0
    assert len(bedrock_calls) == calls
