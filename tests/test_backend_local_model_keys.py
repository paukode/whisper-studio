"""The hybrid `backends` map accepts specific on-device model keys for the two
LLM capabilities (ner, index_llm), not just the haiku/local/gliner sentinels."""

import server.local.registry as reg
import server.local.runtime as rt


def _fake_registry(monkeypatch):
    monkeypatch.setattr(reg, "local_models", lambda: {"local_qwen35_9b": {"label": "Qwen"}})


def test_resolve_map_engine_passes_local_model_keys(monkeypatch):
    from server.infrastructure.oneshot import resolve_map_engine

    _fake_registry(monkeypatch)
    cfg = {"model_mode": "hybrid", "backends": {"index_llm": "local_qwen35_9b"}}
    assert resolve_map_engine(cfg) == "local_qwen35_9b"
    # Unknown values still coerce to haiku (the map step must produce output).
    cfg["backends"]["index_llm"] = "bogus"
    assert resolve_map_engine(cfg) == "haiku"
    # The sentinels are untouched.
    cfg["backends"]["index_llm"] = "local"
    assert resolve_map_engine(cfg) == "local"


def test_one_shot_runs_on_the_picked_local_model(monkeypatch):
    """An explicit model key wins over the resident-following 'local' alias."""
    from server.infrastructure import oneshot

    _fake_registry(monkeypatch)
    monkeypatch.setattr(rt, "is_local_model", lambda key: key == "local_qwen35_9b")
    monkeypatch.setattr(rt, "is_downloaded", lambda key: True)
    seen = {}

    def fake_complete(key, system, user, max_tokens=1500):
        seen["key"] = key
        return "condensed"

    monkeypatch.setattr(rt, "complete", fake_complete)
    out = oneshot.one_shot(
        system="s",
        user="u",
        engine="local_qwen35_9b",
        local_model_key="local_gemma",
        max_tokens=200,
    )
    assert out == "condensed"
    assert seen["key"] == "local_qwen35_9b"  # not the caller's local_model_key


def test_extract_entities_routes_to_local_llm(monkeypatch):
    from server.index import extractor

    _fake_registry(monkeypatch)
    monkeypatch.setattr(rt, "is_downloaded", lambda key: True)
    seen = {}

    def fake_complete(key, system, user, max_tokens=800):
        seen["key"] = key
        return '[{"name": "Acme", "label": "Company"}]'

    monkeypatch.setattr(rt, "complete", fake_complete)
    out = extractor.extract_entities(
        "Acme hired Bob.", labels=["Company", "Person"], backend="local_qwen35_9b"
    )
    assert out == [{"name": "Acme", "label": "Company", "score": 0.8}]
    assert seen["key"] == "local_qwen35_9b"


def test_extract_entities_local_llm_skips_when_model_missing(monkeypatch):
    """A missing model skips LLM NER (never downloads, never falls back to cloud)."""
    from server.index import extractor

    _fake_registry(monkeypatch)
    monkeypatch.setattr(rt, "is_downloaded", lambda key: False)

    def boom(*a, **k):
        raise AssertionError("complete() must not be called when the model is missing")

    monkeypatch.setattr(rt, "complete", boom)
    out = extractor.extract_entities(
        "Acme hired Bob.", labels=["Company", "Person"], backend="local_qwen35_9b"
    )
    assert out == []
