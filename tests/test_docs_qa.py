"""Ask-the-manual (@docs): section extraction, index cache, strict grounding.

The bundled docs site is retrieval-grounded on explicit @docs turns. The
contract under test: sections split on the pages' h2 anchors, the embedded
index caches per docs signature (a changed page rebuilds), retrieval floors
out off-topic questions, and the grounding block either cites pages with
#docspage= links or explicitly instructs the model to decline.
"""

import numpy as np
import pytest

from server import docs_qa
from server.index.query_gate import extract_docs_trigger


@pytest.fixture()
def fake_docs(tmp_path, monkeypatch):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "index.html").write_text("<main><h1>Manual</h1><p>Welcome to the manual.</p></main>")
    (d / "tut-cron.html").write_text(
        "<html><head><title>Cron - Docs</title></head><body><main>"
        "<h1>Scheduled tasks</h1><p>Intro paragraph about cron.</p>"
        '<h2 id="create-a-job">Create a job</h2><p>Describe the schedule in plain language.</p>'
        '<h2 id="run-history">Run history</h2><p>Every run is recorded.</p>'
        "<script>ignored()</script></main></body></html>"
    )
    monkeypatch.setenv("WHISPER_DOCS_DIR", str(d))
    monkeypatch.setattr(docs_qa, "_cache", None)
    # Isolated cache dir for the index files.
    from server.infrastructure import paths as infra_paths

    monkeypatch.setattr(infra_paths, "data_root", lambda: str(tmp_path / "data"))
    return d


def _unit_rows(n: int, dim: int = 8) -> np.ndarray:
    rng = np.random.default_rng(1)
    v = rng.standard_normal((n, dim)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def test_extract_docs_trigger():
    assert extract_docs_trigger("@docs how do I schedule a job") == (
        True,
        "how do I schedule a job",
    )
    assert extract_docs_trigger("how do I schedule a job") == (
        False,
        "how do I schedule a job",
    )
    assert extract_docs_trigger("read the @docsfolder") == (False, "read the @docsfolder")


def test_sections_split_on_h2_anchors(fake_docs):
    sections = docs_qa._extract_sections(str(fake_docs))
    by_title = {s["title"]: s for s in sections}
    assert by_title["Create a job"]["anchor"] == "create-a-job"
    assert by_title["Create a job"]["page"] == "tut-cron.html"
    assert "plain language" in by_title["Create a job"]["text"]
    # The pre-h2 intro rides under the page title with no anchor.
    assert by_title["Scheduled tasks"]["anchor"] == ""
    # Script bodies never leak into section text.
    assert not any("ignored()" in s["text"] for s in sections)


def test_index_builds_once_and_rebuilds_on_change(fake_docs, monkeypatch):
    calls: list[int] = []

    def _fake_embed(texts):
        calls.append(len(texts))
        return _unit_rows(len(texts))

    from server.index import embedder

    monkeypatch.setattr(embedder, "embed_documents", _fake_embed)
    monkeypatch.setattr(embedder, "unload", lambda: None)

    sections, vectors = docs_qa.ensure_index()
    assert len(sections) == len(vectors) and len(calls) == 1

    # Cached: neither the in-memory nor the on-disk path re-embeds.
    docs_qa._cache = None
    sections2, _ = docs_qa.ensure_index()
    assert len(calls) == 1 and len(sections2) == len(sections)

    # A changed page invalidates the signature and rebuilds.
    import os
    import time

    p = fake_docs / "tut-cron.html"
    p.write_text(p.read_text() + "<main><p>more</p></main>")
    os.utime(p, (time.time() + 5, time.time() + 5))
    docs_qa.ensure_index()
    assert len(calls) == 2


def test_grounding_block_cites_docspage_links(fake_docs, monkeypatch):
    hits = [
        {
            "page": "tut-cron.html",
            "page_title": "Scheduled tasks",
            "title": "Create a job",
            "anchor": "create-a-job",
            "text": "Describe the schedule in plain language.",
            "score": 0.8,
        }
    ]
    monkeypatch.setattr(docs_qa, "retrieve", lambda q, k=6: hits)
    block, n = docs_qa.grounding_block("how do I make a cron job?")
    assert n == 1
    assert "#docspage=tut-cron.html&h=create-a-job" in block
    assert "ONLY permitted source" in block


def test_grounding_block_declines_when_nothing_relevant(fake_docs, monkeypatch):
    monkeypatch.setattr(docs_qa, "retrieve", lambda q, k=6: [])
    block, n = docs_qa.grounding_block("what is the meaning of life?")
    assert n == 0
    assert "does not cover" in block and "do not answer" in block


def test_grounding_block_empty_without_bundled_docs(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_DOCS_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr(docs_qa, "_cache", None)
    block, n = docs_qa.grounding_block("anything")
    assert block == "" and n == 0


def test_retrieve_floors_out_offtopic(fake_docs, monkeypatch):
    from server.index import embedder

    monkeypatch.setattr(embedder, "embed_documents", lambda texts: _unit_rows(len(texts)))
    monkeypatch.setattr(embedder, "unload", lambda: None)
    # A query orthogonal-ish to everything scores under the floor → no hits.
    monkeypatch.setattr(embedder, "embed_query", lambda q: np.zeros(8, dtype=np.float32))
    assert docs_qa.retrieve("completely unrelated") == []


def test_route_docs_turn_injects_block_and_skips_index_grounding(monkeypatch):
    import server.docs_qa as docs_mod
    import server.index.pipeline as pipeline_mod
    from tests.golden_harness import (
        FakeBedrockClient,
        msg_end,
        msg_start,
        run_chat_turn,
        text_block,
    )

    index_calls: list = []
    monkeypatch.setattr(
        pipeline_mod,
        "retrieve_grounding",
        lambda *a, **k: index_calls.append(a)
        or ("[ws-ctx]", {"folders": 1, "passages": 1, "sources": []}),
    )
    monkeypatch.setattr(docs_mod, "grounding_block", lambda q: ("[docs-ctx for: " + q + "]", 2))

    client = FakeBedrockClient([[msg_start(), *text_block("from the manual"), *msg_end()]])
    import json as _json

    run_chat_turn(
        monkeypatch,
        client,
        {"question": "@docs how do I schedule a job?", "selected_search_indexes": ["/fake/ws"]},
    )
    sent = _json.dumps(client.requests[0]["messages"])
    assert "[docs-ctx for: how do I schedule a job?]" in sent
    assert "@docs" not in sent  # marker stripped from the prompt
    assert index_calls == []  # workspace grounding sat this turn out
