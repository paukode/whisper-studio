"""Low-severity index fixes.

(2) pipeline stamps the ACTUAL embed model per backend in meta (a Cohere-built
    index must not be labelled with the Qwen3 model id).
(1) the explorer entity page reads the deduped, node-id-keyed ``relations2`` table (joined
    to ``nodes`` for display names).

The embedder and GLiNER are stubbed so no model loads (fast, offline).
"""

import numpy as np
import pytest

from server.index import config, embedder, extractor, paths, pipeline, relstore, store


@pytest.fixture(autouse=True)
def _tmp_index_dir(tmp_path, monkeypatch):
    # Redirect index storage into the test's tmp dir so nothing touches data/.
    monkeypatch.setattr(paths, "INDEX_DATA_DIR", str(tmp_path / "index"))


def _pin_backend(monkeypatch, embed_backend: str) -> None:
    """Pin per-capability backends; only the embed backend varies across tests."""
    from server.infrastructure import model_mode

    caps = {"embed": embed_backend, "rerank": "qwen3", "ner": "gliner", "index_llm": "local"}
    monkeypatch.setattr(
        model_mode, "resolve_backend", lambda cap, config=None: caps.get(cap, "qwen3")
    )


def _unit(seed: int, dim: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _stub_models(monkeypatch, dim: int) -> None:
    monkeypatch.setattr(
        embedder,
        "embed_documents",
        lambda texts: (
            np.vstack([_unit(hash(t) % 9999, dim) for t in texts])
            if texts
            else np.zeros((0, dim), np.float32)
        ),
    )
    monkeypatch.setattr(extractor, "extract_entities", lambda text, *a, **k: [])
    monkeypatch.setattr(embedder, "unload", lambda: None)
    monkeypatch.setattr(extractor, "unload", lambda: None)


# ── Fix (2): embed_model stamped per backend ─────────────────────────────────


def test_cohere_index_stamps_cohere_embed_model(tmp_path, monkeypatch):
    """A Cohere-built index records the Cohere model id, not the Qwen3 default."""
    # Guard: the two model ids must actually differ, else the test proves nothing.
    assert config.COHERE_EMBED_MODEL_ID != config.EMBED_MODEL
    _pin_backend(monkeypatch, "cohere")
    _stub_models(monkeypatch, config.COHERE_EMBED_DIM)
    ws = tmp_path / "proj_cohere"
    ws.mkdir()
    (ws / "a.md").write_text("some indexable content about widgets and gadgets")

    pipeline.build(str(ws))
    meta = store.get_meta(str(ws))

    assert meta["embed_backend"] == "cohere"
    assert meta["embed_model"] == config.COHERE_EMBED_MODEL_ID


def test_qwen3_index_stamps_qwen3_embed_model(tmp_path, monkeypatch):
    """The local (qwen3) path still stamps the Qwen3 model id."""
    _pin_backend(monkeypatch, "qwen3")
    _stub_models(monkeypatch, config.EMBED_DIM)
    ws = tmp_path / "proj_qwen"
    ws.mkdir()
    (ws / "a.md").write_text("some indexable content about widgets and gadgets")

    pipeline.build(str(ws))
    meta = store.get_meta(str(ws))

    assert meta["embed_backend"] == "qwen3"
    assert meta["embed_model"] == config.EMBED_MODEL


# ── Fix (1): the entity page reads relations2 ────────────────────────────────


def _seed_two_entities(ws: str) -> None:
    """One file mentioning Bob and Acme, so both nodes exist for the relation."""
    ents = [{"name": "Bob", "label": "person"}, {"name": "Acme", "label": "organization"}]
    store.replace_file(
        ws,
        "a.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "Bob works at Acme",
                "vec": _unit(1, config.EMBED_DIM),
                "entities": ents,
            }
        ],
    )


def test_entity_page_reads_relations2_when_present(monkeypatch):
    """A relation stored ONLY in relations2 surfaces on the entity page."""
    _pin_backend(monkeypatch, "qwen3")
    ws = "/fake/ws-rel2-graph"
    _seed_two_entities(ws)
    relstore.set_file_relations_v2(
        ws,
        "a.md",
        [
            {
                "source": "Bob",
                "target": "Acme",
                "predicate": "works_at",
                "strength": 4.0,
                "evidence": "Bob works at Acme",
                "start_line": 1,
                "end_line": 1,
            }
        ],
    )
    facts = store.explore_entity(ws, "bob")["facts"]
    rels = [f for f in facts if f["predicate"] == "works_at"]
    assert rels, "relations2 fact should surface on the entity page"
    assert rels[0]["other"] == "Acme" and rels[0]["direction"] == "out"
    assert rels[0]["cite"]["evidence"] == "Bob works at Acme"
