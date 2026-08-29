"""Grounding intent gate + @index trigger.

A contentless message ("hey") used to trigger index retrieval on every turn:
it embedded as cosine noise, rode the floor-exempt BM25 keyword leg into any
document that literally contains the word (an old meeting transcript), and the
injected passages then read as the task itself — a small local model would
invent an analysis out of them instead of greeting back.

Three layers under test:
1. query_gate.should_ground — smalltalk never triggers automatic retrieval;
   anything with a content-bearing token (including unknown/foreign words) does.
2. The chat route — skips retrieval entirely for smalltalk, and an explicit
   ``@index`` mention forces it (and is stripped from the model prompt).
3. retrieve_grounding — the keyword leg only contributes when the query
   carries a content word, so a greeting literally present in the corpus can't
   ride BM25 past the relevance floor.
"""

import json

import numpy as np
import pytest

from server.index import paths, pipeline, store
from server.index.config import EMBED_DIM
from server.index.query_gate import extract_index_trigger, has_content_word, should_ground


@pytest.fixture(autouse=True)
def _tmp_index_dir(tmp_path, monkeypatch):
    # Redirect index storage into the test's tmp dir so nothing touches data/,
    # and pin the per-capability backends to local regardless of config mode.
    monkeypatch.setattr(paths, "INDEX_DATA_DIR", str(tmp_path / "index"))
    from server.infrastructure import model_mode

    _local = {"embed": "qwen3", "rerank": "qwen3", "ner": "gliner", "index_llm": "local"}
    monkeypatch.setattr(model_mode, "resolve_backend", lambda cap, config=None: _local[cap])


def _onehot(i: int) -> np.ndarray:
    v = np.zeros(EMBED_DIM, dtype=np.float32)
    v[i] = 1.0
    return v


# ── should_ground / has_content_word ─────────────────────────────────────────


def test_smalltalk_does_not_ground():
    for q in (
        "hey",
        "Hey!",
        "hello there",
        "hi",
        "yo",
        "thanks!",
        "thank you",
        "ok cool",
        "good morning",
        "yes please",
        "no",
        "don't",
        "???",
        "",
        "   ",
    ):
        assert not should_ground(q), q


def test_content_questions_ground():
    for q in (
        "compare the BusinessObjects query with the Glue catalog",
        "what about Ada?",  # follow-up: the name is the content
        "why is staging failing",
        "hey, why is staging failing?",  # greeting + real question still grounds
        "cześć",  # unknown/foreign token counts as content by design
        "invoice 4711",
    ):
        assert should_ground(q), q


def test_has_content_word_matches_gate():
    assert not has_content_word("hey there")
    assert has_content_word("hey ZZQX")


# ── @index trigger parsing ────────────────────────────────────────────────────


def test_extract_index_trigger():
    assert extract_index_trigger("what is staging") == (False, "what is staging")
    assert extract_index_trigger("@index what is staging") == (True, "what is staging")
    assert extract_index_trigger("@index: what is staging") == (True, "what is staging")
    assert extract_index_trigger("@Index what is staging") == (True, "what is staging")
    assert extract_index_trigger("compare @index staging docs") == (True, "compare staging docs")
    assert extract_index_trigger("@index") == (True, "")
    # An email-like token is not a trigger, and @indexes is a different word.
    assert extract_index_trigger("mail foo@index.com") == (False, "mail foo@index.com")
    assert extract_index_trigger("rebuild @indexes now") == (False, "rebuild @indexes now")


# ── keyword leg requires a content word ──────────────────────────────────────


def test_keyword_leg_gated_for_contentless_query(monkeypatch):
    from server.index import embedder

    ws = "/fake/ws-kwgate"
    store.replace_file(
        ws,
        "notes.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 3,
                "text": "hey team quick sync about the ZZQX budget",
                "vec": _onehot(0),
                "entities": [],
            }
        ],
    )
    # Dense leg is inert: the query embeds orthogonally to the chunk (cosine 0,
    # under the floor), so anything returned must come via the keyword leg.
    monkeypatch.setattr(embedder, "embed_query", lambda q: _onehot(1))

    # "hey" appears verbatim in the document, but a contentless query must not
    # ride BM25 into grounding.
    g, meta = pipeline.retrieve_grounding([ws], "hey", return_meta=True)
    assert g == ""
    assert meta["passages"] == 0

    # A content word still reaches the same chunk through the keyword leg.
    g = pipeline.retrieve_grounding([ws], "ZZQX")
    assert "notes.md" in g


# ── chat route: gate + @index force ──────────────────────────────────────────


def _spy_grounding(monkeypatch):
    import server.index.pipeline as pipeline_mod

    calls: list[tuple] = []

    def _spy(indexes, question, **kw):
        calls.append((indexes, question))
        meta = {"folders": 1, "passages": 1, "sources": []}
        return "[ctx]", meta

    monkeypatch.setattr(pipeline_mod, "retrieve_grounding", _spy)
    return calls


def test_route_skips_grounding_for_smalltalk(monkeypatch):
    from tests.golden_harness import (
        FakeBedrockClient,
        msg_end,
        msg_start,
        run_chat_turn,
        text_block,
    )

    calls = _spy_grounding(monkeypatch)
    client = FakeBedrockClient([[msg_start(), *text_block("hey!"), *msg_end()]])
    lines = run_chat_turn(
        monkeypatch,
        client,
        {"question": "hey", "selected_search_indexes": ["/fake/ws"]},
    )

    assert calls == []  # no retrieval: no embedder call, no latency
    frames = [json.loads(ln) for ln in lines if ln != "[DONE]"]
    assert not any("grounding" in f for f in frames)


def test_route_at_index_forces_grounding_and_strips_marker(monkeypatch):
    from tests.golden_harness import (
        FakeBedrockClient,
        msg_end,
        msg_start,
        run_chat_turn,
        text_block,
    )

    calls = _spy_grounding(monkeypatch)
    client = FakeBedrockClient([[msg_start(), *text_block("grounded"), *msg_end()]])
    lines = run_chat_turn(
        monkeypatch,
        client,
        {"question": "@index hey", "selected_search_indexes": ["/fake/ws"]},
    )

    # Forced past the smalltalk gate, retrieval saw the marker-free question.
    assert calls == [(["/fake/ws"], "hey")]
    frames = [json.loads(ln) for ln in lines if ln != "[DONE]"]
    assert any("grounding" in f for f in frames)
    # The marker never reaches the model prompt; the grounding block does.
    sent = json.dumps(client.requests[0]["messages"])
    assert "@index" not in sent
    assert "[ctx]" in sent


def test_route_at_index_falls_back_to_all_indexes(monkeypatch):
    import server.index.store as index_store
    from tests.golden_harness import (
        FakeBedrockClient,
        msg_end,
        msg_start,
        run_chat_turn,
        text_block,
    )

    calls = _spy_grounding(monkeypatch)
    monkeypatch.setattr(index_store, "list_indexed_workspaces", lambda: ["/fake/ws-auto"])
    client = FakeBedrockClient([[msg_start(), *text_block("grounded"), *msg_end()]])
    run_chat_turn(
        monkeypatch,
        client,
        # Explicit empty selection (e.g. a connected workspace deprioritized
        # the index); @index overrides it for this one turn.
        {"question": "@index what is the etl design", "selected_search_indexes": []},
    )

    assert calls == [(["/fake/ws-auto"], "what is the etl design")]
