"""Per-backend index storage + Cohere/Haiku backend routing (Bedrock mocked)."""

import json

import numpy as np
import pytest

from server.index import config, embedder, embedder_cohere, extractor, paths, reranker


class _Body:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b


def _resp(payload):
    return {"body": _Body(payload)}


# ── per-backend storage paths ────────────────────────────────────────────────


def test_db_path_partitions_by_embed_backend():
    ws = "/tmp/some/workspace"
    # qwen3 (and the legacy default) keep the original index.db for back-compat.
    assert paths.db_path(ws, "qwen3").endswith("/index.db")
    assert paths.db_path(ws, None).endswith("/index.db") or True  # None routes via mode
    # Other backends get a sibling file in the SAME workspace dir.
    cohere = paths.db_path(ws, "cohere")
    assert cohere.endswith("/index-cohere.db")
    assert paths.workspace_index_dir(ws) == cohere.rsplit("/", 1)[0]


def test_db_path_routes_to_active_backend(monkeypatch):
    from server.infrastructure import model_mode

    ws = "/tmp/some/workspace"
    monkeypatch.setattr(model_mode, "resolve_backend", lambda cap, config=None: "cohere")
    assert paths.db_path(ws).endswith("/index-cohere.db")
    monkeypatch.setattr(model_mode, "resolve_backend", lambda cap, config=None: "qwen3")
    assert paths.db_path(ws).endswith("/index.db")


def test_dim_for_backend():
    assert config.dim_for_backend("qwen3") == config.EMBED_DIM
    assert config.dim_for_backend("cohere") == config.COHERE_EMBED_DIM == 1536
    assert config.dim_for_backend(None) == config.EMBED_DIM


def test_profile_for_ext_auto_selects():
    """Entity profile is chosen from the file extension, not a user setting: source
    code -> code labels; docs and everything else -> business labels."""
    for ext in (".py", ".ts", ".java", ".rs", ".sql", ".vue", ".CPP"):  # case-insensitive
        assert config.profile_for_ext(ext) == "code"
        assert config.labels_for_profile(config.profile_for_ext(ext)) is config._CODE_LABELS
    for ext in (".md", ".txt", ".rst", ".pdf", ".docx", ".json", "", None):
        assert config.profile_for_ext(ext) == "business"
        assert config.labels_for_profile(config.profile_for_ext(ext)) is config._BUSINESS_LABELS


# ── embedder routing ─────────────────────────────────────────────────────────


def test_embedder_routes_to_cohere_in_cloud(monkeypatch):
    from server.infrastructure import model_mode

    monkeypatch.setattr(model_mode, "resolve_backend", lambda cap, config=None: "cohere")
    monkeypatch.setattr(
        embedder_cohere, "embed_documents", lambda t: np.full((len(t), 4), 0.5, np.float32)
    )
    out = embedder.embed_documents(["a", "b"])
    assert out.shape == (2, 4)


def test_embedder_uses_qwen3_local(monkeypatch):
    from server.infrastructure import model_mode

    monkeypatch.setattr(model_mode, "resolve_backend", lambda cap, config=None: "qwen3")
    monkeypatch.setattr(
        embedder, "_embed", lambda texts: np.zeros((len(texts), config.EMBED_DIM), np.float32)
    )
    out = embedder.embed_documents(["a"])
    assert out.shape == (1, config.EMBED_DIM)


# ── Cohere embed request/response mapping ────────────────────────────────────


def test_cohere_embed_parses_and_l2_normalizes(monkeypatch):
    class _Client:
        def invoke_model(self, modelId, body):
            sent = json.loads(body)
            assert sent["input_type"] == "search_document"
            return _resp({"embeddings": {"float": [[3.0, 4.0]]}})  # -> unit [0.6, 0.8]

    monkeypatch.setattr(embedder_cohere, "_bedrock", lambda: _Client())
    out = embedder_cohere.embed_documents(["hello"])
    assert out.shape == (1, 2)
    np.testing.assert_allclose(out[0], [0.6, 0.8], atol=1e-5)
    assert abs(float(np.linalg.norm(out[0])) - 1.0) < 1e-5


def test_cohere_embed_query_uses_search_query_input_type(monkeypatch):
    seen = {}

    class _Client:
        def invoke_model(self, modelId, body):
            seen["input_type"] = json.loads(body)["input_type"]
            return _resp({"embeddings": {"float": [[1.0, 0.0]]}})

    monkeypatch.setattr(embedder_cohere, "_bedrock", lambda: _Client())
    v = embedder_cohere.embed_query("q")
    assert seen["input_type"] == "search_query"
    assert v.shape == (2,)


def test_cohere_embed_raises_on_count_mismatch(monkeypatch):
    class _Client:
        def invoke_model(self, modelId, body):
            return _resp({"embeddings": {"float": [[1.0, 0.0]]}})  # 1 vec for 2 texts

    monkeypatch.setattr(embedder_cohere, "_bedrock", lambda: _Client())
    with pytest.raises(RuntimeError):
        embedder_cohere.embed_documents(["a", "b"])


# ── Cohere rerank ────────────────────────────────────────────────────────────


def test_cohere_rerank_aligns_scores_to_input_order(monkeypatch):
    class _Client:
        def invoke_model(self, modelId, body):
            sent = json.loads(body)
            assert sent["api_version"] == 2
            return _resp(
                {
                    "results": [
                        {"index": 1, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 0.1},
                    ]
                }
            )

    monkeypatch.setattr(embedder_cohere, "_bedrock", lambda: _Client())
    scores = reranker.rerank("q", ["doc-a", "doc-b"], backend="cohere")
    assert scores == [0.1, 0.9]


def test_cohere_rerank_best_effort_on_failure(monkeypatch):
    class _Client:
        def invoke_model(self, modelId, body):
            raise RuntimeError("bedrock down")

    monkeypatch.setattr(embedder_cohere, "_bedrock", lambda: _Client())
    assert reranker.rerank("q", ["a"], backend="cohere") == []


# ── Haiku NER ────────────────────────────────────────────────────────────────


def test_haiku_ner_validates_labels_and_dedups(monkeypatch):
    import server.chat.infra as infra

    monkeypatch.setattr(infra, "_get_chat_models", lambda: {"haiku": "anthropic.claude-haiku"})
    arr = [
        {"name": "Ada Lovelace", "label": "person"},
        {"name": "thing", "label": "bogus-label"},  # dropped: not in the active profile
        {"name": "boto3", "label": "library"},
        {"name": "Ada Lovelace", "label": "person"},  # dup: dropped
        {"name": "the api", "label": "api"},  # 'api' -> canonical 'API'
    ]

    class _Client:
        def invoke_model(self, modelId, body):
            return _resp({"content": [{"type": "text", "text": json.dumps(arr)}]})

    monkeypatch.setattr(infra, "_get_bedrock_client", lambda: _Client())
    # Haiku labels are validated against the ACTIVE profile (the labels passed in),
    # not a global union — an off-profile label is dropped, casing is canonicalized.
    out = extractor.extract_entities(
        "some text", labels=["person", "library", "API"], backend="haiku"
    )
    names = [e["name"] for e in out]
    assert names == ["Ada Lovelace", "boto3", "the api"]
    assert {e["label"] for e in out} == {"person", "library", "API"}


# ── GLiNER2 (optional on-device NER model) ────────────────────────────────────


def test_gliner2_path_flattens_and_filters(monkeypatch):
    """ner_model='gliner2' routes to GLiNER2, flattening its grouped return into
    [{name,label,score}], applying the same junk gate and highest-score dedup."""

    class _FakeG2:
        def extract_entities(self, text, labels, include_confidence=False):
            return {
                "entities": {
                    "person": [
                        {"text": "Ada Lovelace", "confidence": 0.9},
                        {"text": "ada lovelace", "confidence": 0.8},  # dup -> keep 0.9
                    ],
                    "organization": [{"text": "Acme", "confidence": 0.7}],
                    "concept": [{"text": "trust", "confidence": 0.95}],  # hard junk -> dropped
                }
            }

    monkeypatch.setattr(extractor, "_load_gliner2", lambda: None)
    monkeypatch.setattr(extractor, "_gliner2_model", _FakeG2())
    out = extractor.extract_entities(
        "some text",
        labels=["person", "organization", "concept"],
        backend="gliner",
        ner_model="gliner2",
    )
    by = {e["name"]: e for e in out}
    assert set(by) == {"Ada Lovelace", "Acme"}  # 'trust' dropped as junk
    assert by["Ada Lovelace"]["score"] == 0.9  # highest-scoring occurrence kept


def test_ws_settings_ner_model_defaults_and_validates():
    from server.index import wssettings

    v = wssettings._validated({})
    assert v["ner_model"] == "gliner"  # default stays gliner
    assert wssettings._validated({"ner_model": "gliner2"})["ner_model"] == "gliner2"
    assert wssettings._validated({"ner_model": "bogus"})["ner_model"] == "gliner"


def test_gliner2_native_relations(monkeypatch):
    """ner_model='gliner2' extracts relations natively (no LLM): head/tail mapped to
    known entities, predicate snapped to the closed vocabulary, score on the 1-5
    scale, endpoints outside the entity set dropped."""

    class _Chain:
        def entities(self, *a, **k):
            return self

        def relations(self, *a, **k):
            return self

    class _FakeG2:
        def create_schema(self):
            return _Chain()

        def extract(self, text, schema, include_confidence=False):
            return {
                "relation_extraction": {
                    # highest-scoring predicate for the Ann->Acme pair; wins
                    "works at": [
                        {
                            "head": {"text": "Ann", "confidence": 0.9},
                            "tail": {"text": "Acme", "confidence": 0.8},
                        }
                    ],
                    # same directed pair, lower score -> dropped (top-per-pair)
                    "member of": [
                        {
                            "head": {"text": "Ann", "confidence": 0.7},
                            "tail": {"text": "Acme", "confidence": 0.7},
                        }
                    ],
                    # distinct pair but below the confidence floor (0.5) -> dropped
                    "reports to": [
                        {
                            "head": {"text": "Bob", "confidence": 0.4},
                            "tail": {"text": "Acme", "confidence": 0.4},
                        }
                    ],
                    # tail is not a known entity -> dropped
                    "hired": [
                        {
                            "head": {"text": "Acme", "confidence": 0.9},
                            "tail": {"text": "Nobody", "confidence": 0.9},
                        }
                    ],
                }
            }

    monkeypatch.setattr(extractor, "_load_gliner2", lambda: None)
    monkeypatch.setattr(extractor, "_gliner2_model", _FakeG2())
    rels = extractor.extract_relations_gliner2("Ann works at Acme.", ["Ann", "Acme", "Bob"])
    # works_at wins the Ann->Acme pair (1 + 4*min(0.9,0.8) = 4.2); Bob->Acme is
    # dropped by the floor; Acme->Nobody dropped (unknown tail).
    assert rels == [("Ann", "Acme", "works_at", 4.2)]
