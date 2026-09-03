"""Auto-grounding self-mute.

Top-k nearest ALWAYS returns something, so an off-topic question ("how is the
weather?") used to inject a dozen least-off-topic passages and a graph-hop
tail, and the answer chip claimed the reply was "grounded" in them. Unforced
turns now judge topicality by the best dense cosine: under the per-backend
on-topic floor the dense leg and its graph-hop expansion contribute nothing,
and with no literal keyword/entity hit either, nothing is injected at all.
Forced (@index) turns keep the loose noise floor.
"""

import numpy as np
import pytest

from server.index import paths, pipeline, store
from server.index.config import EMBED_DIM, GROUND_ON_TOPIC_FLOORS


@pytest.fixture(autouse=True)
def _tmp_index_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "INDEX_DATA_DIR", str(tmp_path / "index"))
    from server.infrastructure import model_mode

    _local = {"embed": "qwen3", "rerank": "qwen3", "ner": "gliner", "index_llm": "local"}
    monkeypatch.setattr(model_mode, "resolve_backend", lambda cap, config=None: _local[cap])


def _unit(i: int) -> np.ndarray:
    v = np.zeros(EMBED_DIM, dtype=np.float32)
    v[i] = 1.0
    return v


def _vec_with_cosine(cos: float) -> np.ndarray:
    """Unit vector whose cosine against _unit(0) is exactly ``cos``."""
    v = np.zeros(EMBED_DIM, dtype=np.float32)
    v[0] = cos
    v[1] = np.sqrt(1.0 - cos * cos)
    return v


FLOOR = GROUND_ON_TOPIC_FLOORS["qwen3"]


def _index_chunk(ws: str, cos: float, text: str = "quarterly budget planning notes") -> None:
    store.replace_file(
        ws,
        "notes.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 3,
                "text": text,
                "vec": _vec_with_cosine(cos),
                "entities": [],
            }
        ],
    )


def _spy_expand(monkeypatch):
    calls: list[list[int]] = []
    real = store.expand

    def _spy(ws_path, chunk_ids, limit=4):
        calls.append(list(chunk_ids))
        return real(ws_path, chunk_ids, limit=limit)

    monkeypatch.setattr(store, "expand", _spy)
    return calls


def test_auto_turn_mutes_off_topic_corpus(monkeypatch):
    """Weak best cosine + no literal hits -> nothing injected, hop never runs."""
    ws = "/fake/ws-mute"
    _index_chunk(ws, cos=FLOOR - 0.08)
    monkeypatch.setattr(pipeline.embedder, "embed_query", lambda q: _unit(0))
    expands = _spy_expand(monkeypatch)

    g, meta = pipeline.retrieve_grounding([ws], "how is the weather in krakow", return_meta=True)

    assert g == ""
    assert meta["passages"] == 0
    assert meta["folders"] == 1
    assert meta["dense_muted"] is True
    assert meta["best_score"] == pytest.approx(FLOOR - 0.08, abs=0.01)
    assert expands == []


def test_forced_turn_keeps_weak_matches(monkeypatch):
    """@index keeps today's aggressive behavior: the loose noise floor rules."""
    ws = "/fake/ws-forced"
    _index_chunk(ws, cos=FLOOR - 0.08)
    monkeypatch.setattr(pipeline.embedder, "embed_query", lambda q: _unit(0))
    expands = _spy_expand(monkeypatch)

    g, meta = pipeline.retrieve_grounding(
        [ws], "how is the weather in krakow", return_meta=True, forced=True
    )

    assert "notes.md" in g
    assert meta["passages"] >= 1
    assert meta["dense_muted"] is False
    assert expands, "forced turns still expand the graph hop from floored seeds"


def test_on_topic_dense_grounds_and_expands(monkeypatch):
    ws = "/fake/ws-ontopic"
    _index_chunk(ws, cos=0.9)
    monkeypatch.setattr(pipeline.embedder, "embed_query", lambda q: _unit(0))
    expands = _spy_expand(monkeypatch)

    g, meta = pipeline.retrieve_grounding([ws], "budget planning status", return_meta=True)

    assert "notes.md" in g
    assert meta["dense_muted"] is False
    assert meta["best_score"] == pytest.approx(0.9, abs=0.01)
    assert len(expands) == 1, "hop runs once, from the on-topic seed"


def test_keyword_hit_survives_dense_mute(monkeypatch):
    """A literal term the corpus contains still grounds when dense is muted:
    exact-match legs are the rescue path for identifiers."""
    ws = "/fake/ws-kwrescue"
    _index_chunk(ws, cos=0.0, text="ZZQX7 rollout checklist for the region")
    monkeypatch.setattr(pipeline.embedder, "embed_query", lambda q: _unit(0))
    expands = _spy_expand(monkeypatch)

    g, meta = pipeline.retrieve_grounding([ws], "ZZQX7", return_meta=True)

    assert "notes.md" in g
    assert meta["passages"] >= 1
    assert meta["dense_muted"] is True
    assert expands == [], "no on-topic dense seeds -> no graph hop"


def test_weak_seeds_do_not_expand_even_when_best_is_on_topic(monkeypatch):
    """Auto turns hop only from seeds that themselves clear the on-topic floor;
    a weaker second match that merely survives the noise floors must not drag
    its entity neighborhood in. Scores: top 0.45 clears the 0.40 on-topic
    floor (turn not muted); second 0.30 survives the noise floors (absolute
    0.15, relative 0.55 x 0.45 = 0.2475) but is not an on-topic seed."""
    ws = "/fake/ws-weakseed"
    strong = {
        "start_line": 1,
        "end_line": 3,
        "text": "quarterly budget planning notes",
        "vec": _vec_with_cosine(FLOOR + 0.05),
        "entities": [],
    }
    weak = {
        "start_line": 5,
        "end_line": 8,
        "text": "unrelated cafeteria menu",
        "vec": _vec_with_cosine(FLOOR - 0.10),
        "entities": [],
    }
    store.replace_file(ws, "notes.md", {"hash": "h", "size": 1, "mtime": 1.0}, [strong, weak])
    monkeypatch.setattr(pipeline.embedder, "embed_query", lambda q: _unit(0))
    expands = _spy_expand(monkeypatch)

    _, meta = pipeline.retrieve_grounding([ws], "budget planning status", return_meta=True)

    assert meta["dense_muted"] is False
    assert meta["passages"] == 2, "the weak match still grounds; it just doesn't seed the hop"
    assert len(expands) == 1
    assert len(expands[0]) == 1, "only the on-topic seed feeds the hop"
