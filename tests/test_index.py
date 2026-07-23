"""Tests for the workspace index engine — chunking, the SQLite+numpy store,
graph expansion, and incremental change detection. The embedder and GLiNER are
stubbed so no model loads (fast, deterministic, offline)."""

import numpy as np
import pytest

from server.index import paths, pipeline, salience, store
from server.index.chunker import chunk_text
from server.index.config import EMBED_DIM


@pytest.fixture(autouse=True)
def _tmp_index_dir(tmp_path, monkeypatch):
    # Redirect index storage into the test's tmp dir so nothing touches data/.
    monkeypatch.setattr(paths, "INDEX_DATA_DIR", str(tmp_path / "index"))
    # These tests exercise the on-device pipeline (Qwen3 embedder / GLiNER), so
    # pin the per-capability backends to local regardless of the ambient config
    # mode (the default mode is cloud, which would route to Cohere/Bedrock).
    from server.infrastructure import model_mode

    _local = {"embed": "qwen3", "rerank": "qwen3", "ner": "gliner", "index_llm": "local"}
    monkeypatch.setattr(model_mode, "resolve_backend", lambda cap, config=None: _local[cap])


def _unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


# ── chunker ──────────────────────────────────────────────────────────────────


def test_chunk_text_line_ranges_and_overlap():
    text = "\n".join(f"line {i}" for i in range(1, 401))  # 400 plain lines, no boundaries
    chunks = chunk_text(text, max_tokens=20, overlap_tokens=4)
    assert len(chunks) > 1
    assert chunks[0]["start_line"] == 1
    # contiguous + overlapping: each chunk starts at/before the previous end.
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert b["start_line"] <= a["end_line"] + 1
    assert chunks[-1]["end_line"] == 400  # full coverage


def test_chunk_text_empty():
    assert chunk_text("") == []
    assert chunk_text("   \n  \n") == []


def test_chunk_splits_at_code_definitions():
    text = (
        "def alpha():\n    a = 1\n    return a\n"
        "\n"
        "def beta():\n    b = 2\n    return b\n"
        "\n"
        "def gamma():\n    c = 3\n    return c\n"
    )
    chunks = chunk_text(text, max_tokens=20, overlap_tokens=2)
    firsts = [c["text"].splitlines()[0] for c in chunks]
    assert firsts == ["def alpha():", "def beta():", "def gamma():"]  # each chunk = one function


def test_chunk_splits_at_markdown_headings():
    md = "# Intro\nsome intro text\n\n# Details\nmore detail text\n\n# End\nfinal text\n"
    chunks = chunk_text(md, max_tokens=12, overlap_tokens=2)
    firsts = [c["text"].splitlines()[0] for c in chunks]
    assert firsts[0].startswith("# Intro")
    assert any(f.startswith("# Details") for f in firsts)


def test_section_path_builds_heading_breadcrumb():
    from server.index.chunker import section_path

    md = "# Overview\nintro\n\n## Q3 results\nrevenue grew\n\n## Q4 plan\nnext steps\n"
    # line 5 ("revenue grew") sits under Overview > Q3 results
    assert section_path(md, 5) == "Overview > Q3 results"
    # a deeper heading replaces its siblings, keeping the ancestor
    assert section_path(md, 8) == "Overview > Q4 plan"
    # before any heading, the breadcrumb is empty
    assert section_path("plain text\nmore\n", 2) == ""


# ── store: vector search + graph ─────────────────────────────────────────────


def test_store_search_ranks_by_cosine():
    ws = "/fake/ws-search"
    va, vb = _unit(1), _unit(2)
    store.replace_file(
        ws,
        "a.py",
        {"hash": "h1", "size": 1, "mtime": 1.0},
        [{"start_line": 1, "end_line": 2, "text": "alpha", "vec": va, "entities": []}],
    )
    store.replace_file(
        ws,
        "b.py",
        {"hash": "h2", "size": 1, "mtime": 1.0},
        [{"start_line": 1, "end_line": 2, "text": "beta", "vec": vb, "entities": []}],
    )
    res = store.search(ws, va, k=2)
    assert res[0]["path"] == "a.py"  # closest to va
    assert res[0]["score"] > res[1]["score"]


def test_store_graph_expand_via_shared_entity():
    ws = "/fake/ws-graph"
    ent = [{"name": "PaymentService", "label": "service"}]
    store.replace_file(
        ws,
        "a.py",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "uses PaymentService",
                "vec": _unit(1),
                "entities": ent,
            }
        ],
    )
    store.replace_file(
        ws,
        "b.py",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "also PaymentService",
                "vec": _unit(2),
                "entities": ent,
            }
        ],
    )
    a_chunk = store.search(ws, _unit(1), k=1)[0]
    related = store.expand(ws, [a_chunk["chunk_id"]], limit=5)
    assert any(r["path"] == "b.py" for r in related)
    assert related[0]["shared_entities"] >= 1


# ── entity salience (statistical noise defense) ──────────────────────────────


def _ent(name, label, score=0.9):
    return {"name": name, "label": label, "score": score}


def test_is_hard_junk_rejects_code_and_generics_keeps_real_names():
    junk = [
        ("tool_name", "person"),
        ("user_first_name", "person"),
        ("get_stock_price", "function"),
        ("requirements.txt", "file"),
        ("company.com", "organization"),
        ("langchain.community", "organization"),
        ("getStockPrice", "technology"),
        ("I", "person"),
        ("users", "person"),
        ("trust", "concept"),
        ("firma", "organization"),  # Polish generic
        ("umowy", "document"),  # Polish generic
        ("person", "person"),  # label echo
    ]
    keep = [
        ("Amazon", "organization"),
        ("Ada Lovelace", "person"),
        ("Acme", "organization"),
        ("NPS", "metric"),
        ("Łukasz Kowalski", "person"),  # Polish real name
        ("My CV.pdf", "document"),  # file extension allowed for the document label
    ]
    for n, lbl in junk:
        assert salience.is_hard_junk(n, lbl) is True, f"{n!r} should be junk"
    for n, lbl in keep:
        assert salience.is_hard_junk(n, lbl) is False, f"{n!r} should be kept"
    # proper nouns score high, single lowercase words low, junk zero.
    assert salience.shape_score("Amazon", "organization") == 1.0
    assert salience.shape_score("meeting", "event") == 0.3
    assert salience.shape_score("trust", "concept") == 0.0


def test_recompute_salience_rewards_rare_over_common_entities():
    ws = "/fake/ws-sal-idf"
    for f in ("r1.md", "r2.md"):  # "Rareco" in 2 chunks (rare -> high IDF)
        store.replace_file(
            ws,
            f,
            {"hash": "h", "size": 1, "mtime": 1.0},
            [
                {
                    "start_line": 1,
                    "end_line": 1,
                    "text": f,
                    "vec": _unit(1),
                    "entities": [_ent("Rareco", "organization")],
                }
            ],
        )
    for i in range(6):  # "Common Bank" in 6 chunks (common -> low IDF)
        store.replace_file(
            ws,
            f"c{i}.md",
            {"hash": "h", "size": 1, "mtime": 1.0},
            [
                {
                    "start_line": 1,
                    "end_line": 1,
                    "text": f"c{i}",
                    "vec": _unit(2),
                    "entities": [_ent("Common Bank", "organization")],
                }
            ],
        )
    salience.recompute(ws)
    import sqlite3 as _sql

    conn = _sql.connect(store.db_path(ws))
    sal = dict(conn.execute("SELECT name, salience FROM nodes").fetchall())
    df = dict(conn.execute("SELECT name, df FROM nodes").fetchall())
    conn.close()
    assert df["Rareco"] == 2 and df["Common Bank"] == 6
    assert sal["Rareco"] > sal["Common Bank"]  # rarity lifts salience


def test_recompute_salience_caps_boilerplate_hub(monkeypatch):
    monkeypatch.setattr(store, "_MAX_NODE_FANOUT", 2)  # boilerplate above 2 chunks
    ws = "/fake/ws-sal-hub"
    for i in range(4):  # "Hub Corp" in 4 chunks -> boilerplate, capped
        store.replace_file(
            ws,
            f"h{i}.md",
            {"hash": "h", "size": 1, "mtime": 1.0},
            [
                {
                    "start_line": 1,
                    "end_line": 1,
                    "text": f"h{i}",
                    "vec": _unit(1),
                    "entities": [_ent("Hub Corp", "organization")],
                }
            ],
        )
    store.replace_file(
        ws,
        "solo.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "solo",
                "vec": _unit(2),
                "entities": [_ent("Solo Corp", "organization")],
            }
        ],
    )
    salience.recompute(ws)
    import sqlite3 as _sql

    conn = _sql.connect(store.db_path(ws))
    sal = dict(conn.execute("SELECT name, salience FROM nodes").fetchall())
    conn.close()
    assert sal["Hub Corp"] <= 0.15  # capped to the junk floor
    assert sal["Solo Corp"] > 0.15  # discriminative entity survives


def test_expand_ranks_by_salience_and_excludes_junk():
    ws = "/fake/ws-expand-weighted"
    # seed shares a rare entity with rareonly, a common one with acmeonly, and a
    # junk one (generic word) with junkonly.
    store.replace_file(
        ws,
        "seed.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "seed",
                "vec": _unit(1),
                "entities": [
                    _ent("Rareco", "organization"),
                    _ent("Acme", "organization"),
                    _ent("data", "concept"),
                ],
            }
        ],
    )
    store.replace_file(
        ws,
        "rareonly.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "rare",
                "vec": _unit(2),
                "entities": [_ent("Rareco", "organization")],
            }
        ],
    )
    for f in ("acmeonly.md", "acme2.md", "acme3.md", "acme4.md"):  # Acme common
        store.replace_file(
            ws,
            f,
            {"hash": "h", "size": 1, "mtime": 1.0},
            [
                {
                    "start_line": 1,
                    "end_line": 1,
                    "text": f,
                    "vec": _unit(3),
                    "entities": [_ent("Acme", "organization")],
                }
            ],
        )
    store.replace_file(
        ws,
        "junkonly.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "junk",
                "vec": _unit(4),
                "entities": [_ent("data", "concept")],
            }
        ],
    )
    salience.recompute(ws)
    seed_id = store.chunks_for_file(ws, "seed.md")[0]["chunk_id"]
    related = store.expand(ws, [seed_id], limit=10)
    paths_ranked = [r["path"] for r in related]
    assert "junkonly.md" not in paths_ranked  # generic-word link is filtered out
    assert paths_ranked.index("rareonly.md") < paths_ranked.index("acmeonly.md")
    by_path = {r["path"]: r for r in related}
    assert by_path["rareonly.md"]["graph_score"] > by_path["acmeonly.md"]["graph_score"]


def test_entity_leg_surfaces_entity_chunks():
    """The entity-linking leg returns chunks of a salient entity named in the query
    (even when the passage wording differs), and ignores junk / unmentioned ones."""
    ws = "/fake/ws-entleg"
    store.replace_file(
        ws,
        "a.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "founder profile and background",
                "vec": _unit(1),
                "entities": [_ent("Acme", "organization")],
            }
        ],
    )
    store.replace_file(
        ws,
        "b.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "unrelated notes",
                "vec": _unit(2),
                "entities": [],
            }
        ],
    )
    salience.recompute(ws)
    hits = salience.entity_leg(ws, "tell me about Acme", k=5)
    assert any(h["path"] == "a.md" for h in hits)
    assert all(h["path"] != "b.md" for h in hits)
    # a query naming no known entity yields nothing
    assert salience.entity_leg(ws, "quarterly revenue trends", k=5) == []


def test_entity_leg_matches_names_with_internal_punctuation():
    """Names carrying '&' / '.' (AT&T, Sp. z o.o.) match the query — the query
    tokenizer drops that punctuation, so the node side must normalize the same way."""
    ws = "/fake/ws-entleg-punct"
    for name in ("AT&T", "S&P 500"):
        store.replace_file(
            ws,
            f"{name.replace(' ', '_').replace('&', 'n')}.md",
            {"hash": "h", "size": 1, "mtime": 1.0},
            [
                {
                    "start_line": 1,
                    "end_line": 1,
                    "text": name + " report",
                    "vec": _unit(hash(name) % 999),
                    "entities": [_ent(name, "organization")],
                }
            ],
        )
    salience.recompute(ws)
    assert salience.entity_leg(ws, "how did AT&T perform", k=5), "AT&T should match"
    assert salience.entity_leg(ws, "the S&P 500 index", k=5), "S&P 500 should match"


def test_node_salience_migration_is_idempotent():
    ws = "/fake/ws-migrate"
    store.replace_file(
        ws,
        "a.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [{"start_line": 1, "end_line": 1, "text": "a", "vec": _unit(1), "entities": []}],
    )
    # _connect runs the idempotent ALTERs every time; a second open must not raise
    # and the new columns must be present.
    for _ in range(2):
        conn = store._connect(ws)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        nc_cols = {r[1] for r in conn.execute("PRAGMA table_info(node_chunks)").fetchall()}
        conn.close()
    assert {"salience", "df", "canonical_id", "llm_verdict", "judged_gen"} <= cols
    assert "score" in nc_cols


def test_fts_polish_diacritics_and_inflection():
    ws = "/fake/ws-fts-pl"
    store.replace_file(
        ws,
        "doc.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 2,
                "text": "Roczny przegląd wyników. Faktura numer 5.",
                "vec": _unit(1),
                "entities": [],
            }
        ],
    )
    # diacritic folding: an ASCII query matches the accented token (ą -> a).
    assert any(r["path"] == "doc.md" for r in store.fts_search(ws, "przeglad", k=5))
    # inflection: "faktury" stems to "faktur*" and matches "Faktura".
    assert any(r["path"] == "doc.md" for r in store.fts_search(ws, "faktury", k=5))


def test_entity_graph_centres_on_an_entity_and_links_its_files():
    """entity_graph powers the 'everything about this person' view: one entity
    node linked to every file that mentions it. Matched case-insensitively and
    the centre shows the stored spelling; file nodes are tagged type=file."""
    ws = "/fake/ws-entity"
    person = [{"name": "Ada Lovelace", "label": "person"}]
    store.replace_file(
        ws,
        "a.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "about Ada Lovelace",
                "vec": _unit(1),
                "entities": person,
            }
        ],
    )
    store.replace_file(
        ws,
        "b.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "Ada Lovelace again",
                "vec": _unit(2),
                "entities": person,
            }
        ],
    )
    g = store.entity_graph(ws, "ada lovelace")  # click carries lowercased name
    centre = g["nodes"][0]
    assert (
        centre["type"] == "entity"
        and centre["name"] == "Ada Lovelace"
        and centre["label"] == "person"
    )
    files = {n["name"] for n in g["nodes"] if n.get("type") == "file"}
    assert files == {"a.md", "b.md"}
    assert len(g["edges"]) == 2 and all(e["source"] == centre["id"] for e in g["edges"])


def test_write_gen_advances_on_index_changes():
    """The write generation bumps on every replace/delete so the sqlite-vec
    index knows when to rebuild (chunk ids are reassigned, so a count alone
    can miss changes)."""
    import sqlite3 as _sql

    ws = "/fake/ws-gen"

    def gen():
        c = _sql.connect(store.db_path(ws))
        try:
            r = c.execute("SELECT value FROM meta WHERE key='write_gen'").fetchone()
            return int(r[0]) if r else 0
        finally:
            c.close()

    store.replace_file(
        ws,
        "a.py",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [{"start_line": 1, "end_line": 1, "text": "x", "vec": _unit(1), "entities": []}],
    )
    g1 = gen()
    store.replace_file(
        ws,
        "b.py",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [{"start_line": 1, "end_line": 1, "text": "y", "vec": _unit(2), "entities": []}],
    )
    g2 = gen()
    store.delete_file(ws, "b.py")
    g3 = gen()
    assert g1 >= 1 and g2 > g1 and g3 > g2


def test_dedupe_entities_merges_textual_variants_not_distinct_spellings(monkeypatch):
    """Lexical layer: case/whitespace/punctuation variants collapse to one node
    (one bubble per person); genuinely different spellings stay separate. The
    semantic pass is disabled here so this stays a fast, model-free unit test of
    the conservative normalizer — semantic merging is covered by the test below."""
    monkeypatch.setattr(store, "ENTITY_SEMANTIC_MERGE", False)
    import sqlite3 as _sql

    ws = "/fake/ws-dedupe"
    for f, nm in [("a.md", "John Doe"), ("b.md", "JOHN DOE"), ("c.md", "J. Doe")]:
        store.replace_file(
            ws,
            f,
            {"hash": "h", "size": 1, "mtime": 1.0},
            [
                {
                    "start_line": 1,
                    "end_line": 1,
                    "text": "x",
                    "vec": _unit(1),
                    "entities": [{"name": nm, "label": "person"}],
                }
            ],
        )

    def person_names():
        c = _sql.connect(store.db_path(ws))
        try:
            return sorted(r[0] for r in c.execute("SELECT name FROM nodes WHERE label='person'"))
        finally:
            c.close()

    assert len(person_names()) == 3  # before: 3 distinct nodes
    assert store.dedupe_entities(ws) == 1  # "John Doe" + "JOHN DOE" → 1 merged away
    after = person_names()
    assert "J. Doe" in after  # distinct spelling NOT merged
    assert sum(1 for n in after if n.lower() == "john doe") == 1
    # the surviving node still links both source files
    g = store.entity_graph(ws, "john doe")
    files = {n["name"] for n in g["nodes"] if n.get("type") == "file"}
    assert {"a.md", "b.md"} <= files


def test_dedupe_entities_semantic_merge_catches_spelling_variants(monkeypatch):
    """Semantic layer: names that are near-duplicate by embedding cosine (within
    a label) merge even when the conservative lexical key can't — e.g.
    "Postgres"/"PostgreSQL" — while dissimilar names in the same label stay
    separate. The embedder is stubbed so the test is deterministic and offline."""
    import sqlite3 as _sql

    # Stub the embedder: Postgres≈PostgreSQL (cosine ~0.99 ≥ 0.92), Redis orthogonal.
    vmap = {
        "Postgres": np.array([1.0, 0.0, 0.0], dtype=np.float32),
        "PostgreSQL": np.array([0.99, 0.141, 0.0], dtype=np.float32),
        "Redis": np.array([0.0, 1.0, 0.0], dtype=np.float32),
    }

    def fake_embed(names):
        rows = [vmap.get(n, np.array([0.3, 0.3, 0.9], dtype=np.float32)) for n in names]
        return np.vstack([v / np.linalg.norm(v) for v in rows])

    monkeypatch.setattr("server.index.embedder.embed_documents", fake_embed)
    monkeypatch.setattr(store, "ENTITY_SEMANTIC_MERGE", True)

    ws = "/fake/ws-sem"
    for f, nm in [("a.md", "Postgres"), ("b.md", "PostgreSQL"), ("c.md", "Redis")]:
        store.replace_file(
            ws,
            f,
            {"hash": "h", "size": 1, "mtime": 1.0},
            [
                {
                    "start_line": 1,
                    "end_line": 1,
                    "text": "x",
                    "vec": _unit(1),
                    "entities": [{"name": nm, "label": "database"}],
                }
            ],
        )

    def db_names():
        c = _sql.connect(store.db_path(ws))
        try:
            return sorted(r[0] for r in c.execute("SELECT name FROM nodes WHERE label='database'"))
        finally:
            c.close()

    assert len(db_names()) == 3  # before: 3 distinct nodes
    assert store.dedupe_entities(ws) == 1  # Postgres + PostgreSQL → 1 merged away
    after = db_names()
    assert "Redis" in after  # dissimilar name NOT merged
    assert sum(1 for n in after if n.lower() in ("postgres", "postgresql")) == 1
    # the surviving merged node still links both source files
    surviving = next(n for n in after if n.lower() in ("postgres", "postgresql"))
    g = store.entity_graph(ws, surviving)
    files = {n["name"] for n in g["nodes"] if n.get("type") == "file"}
    assert {"a.md", "b.md"} <= files


def test_file_graph_assigns_communities(monkeypatch):
    """file_graph nodes carry a `community` index and edge `degree`: two
    entity-disjoint file clusters land in different communities, and nodes within
    a cluster share one. (Leiden when available, networkx Louvain otherwise.)"""
    monkeypatch.setattr(store, "ENTITY_SEMANTIC_MERGE", False)
    ws = "/fake/ws-comm"
    clusters = {
        ("a.md", "b.md", "c.md"): [("Alpha", "product"), ("Beta", "product")],
        ("d.md", "e.md", "f.md"): [("Gamma", "product"), ("Delta", "product")],
    }
    for files, ents in clusters.items():
        for f in files:
            store.replace_file(
                ws,
                f,
                {"hash": "h", "size": 1, "mtime": 1.0},
                [
                    {
                        "start_line": 1,
                        "end_line": 1,
                        "text": "x",
                        "vec": _unit(1),
                        "entities": [{"name": n, "label": lbl} for n, lbl in ents],
                    }
                ],
            )
    g = store.file_graph(ws)
    comm = {n["id"]: n["community"] for n in g["nodes"]}
    c1 = {comm[f] for f in ("a.md", "b.md", "c.md")}
    c2 = {comm[f] for f in ("d.md", "e.md", "f.md")}
    assert len(c1) == 1 and len(c2) == 1  # each cluster internally one community
    assert c1 != c2  # the two clusters differ
    assert all("degree" in n for n in g["nodes"])  # degree present for node sizing


def test_umap_graph_projects_files_to_2d(monkeypatch):
    """umap_graph lays files out by an embedding projection: every node gets
    ux/uy in [0,1] (the semantic-map view) plus the file_graph community. Uses
    the PCA path here (n<5) so it's fast and deterministic — no UMAP/numba."""
    monkeypatch.setattr(store, "ENTITY_SEMANTIC_MERGE", False)
    ws = "/fake/ws-umap"
    for i in range(4):
        store.replace_file(
            ws,
            f"f{i}.md",
            {"hash": "h", "size": 1, "mtime": 1.0},
            [
                {
                    "start_line": 1,
                    "end_line": 1,
                    "text": "x",
                    "vec": _unit(i + 1),
                    "entities": [
                        {"name": "Shared", "label": "product"},
                        {"name": f"E{i % 2}", "label": "product"},
                    ],
                }
            ],
        )
    g = store.umap_graph(ws)
    assert g["layout"] == "umap"
    assert len(g["nodes"]) == 4
    coords = [(n.get("ux"), n.get("uy")) for n in g["nodes"]]
    assert all(x is not None and y is not None for x, y in coords)
    assert all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in coords)


def test_all_workspaces_umap_graph_spans_every_workspace(monkeypatch):
    """The cross-workspace UMAP projects files from ALL indexed workspaces into one
    map (the fix for 'All indexed' + 'UMAP map' previously showing one folder)."""
    monkeypatch.setattr(store, "ENTITY_SEMANTIC_MERGE", False)
    for ws in ("/fake/ws-uall-a", "/fake/ws-uall-b"):
        for i in range(3):
            store.replace_file(
                ws,
                f"f{i}.md",
                {"hash": "h", "size": 1, "mtime": 1.0},
                [
                    {
                        "start_line": 1,
                        "end_line": 1,
                        "text": ws + str(i),
                        "vec": _unit(hash(ws + str(i)) % 9999),
                        "entities": [],
                    }
                ],
            )
        store.set_meta(ws, workspace=ws)  # so list_indexed_workspaces discovers it
    g = store.all_workspaces_umap_graph()
    assert g["layout"] == "umap"
    ids = {n["id"] for n in g["nodes"]}
    assert any("ws-uall-a" in i for i in ids) and any("ws-uall-b" in i for i in ids)
    placed = [n for n in g["nodes"] if "ux" in n and "uy" in n]
    assert len(placed) >= 4  # files from both workspaces projected together
    assert all(0.0 <= n["ux"] <= 1.0 and 0.0 <= n["uy"] <= 1.0 for n in placed)


def test_relations_parse_validates_endpoints():
    """Kept only where both endpoints are known entities; unknowns and self-loops
    dropped. 'employs' snaps to the closed predicate works_at with source/target
    swapped (A employs B -> works_at(B, A))."""
    from server.index.relations import parse_relations

    out = (
        '[{"source":"acme","target":"Bob","type":"employs"},'
        '{"source":"X","target":"Bob","type":"knows"},'
        '{"source":"Acme","target":"Acme","type":"self"}]'
    )
    assert parse_relations(out, ["Acme", "Bob"]) == [("Bob", "Acme", "works_at", 3.0)]


def test_relations_parse_reads_strength_score():
    """A provided strength becomes the relation score (clamped to 1–5, default 3)."""
    from server.index.relations import parse_relations

    out = '[{"source":"Acme","target":"Bob","type":"employs","strength":5}]'
    assert parse_relations(out, ["Acme", "Bob"]) == [("Bob", "Acme", "works_at", 5.0)]


def test_relations_parse_snaps_to_closed_vocabulary():
    """Exact predicates are kept, known synonyms remapped (with a swap when they
    invert), off-vocabulary predicates dropped rather than kept as noise."""
    from server.index.relations import parse_relations

    out = (
        '[{"source":"Ann","target":"Acme","type":"works_at"},'  # exact -> kept
        '{"source":"Ann","target":"Bob","type":"manages"},'  # synonym+swap -> reports_to(Bob,Ann)
        '{"source":"Ann","target":"Acme","type":"synergizes_with"}]'  # unknown -> dropped
    )
    assert parse_relations(out, ["Ann", "Bob", "Acme"]) == [
        ("Ann", "Acme", "works_at", 3.0),
        ("Bob", "Ann", "reports_to", 3.0),
    ]


def test_relations2_facts_and_citation():
    """relations2 stores node-id-keyed facts; facts_for_entity aggregates them with
    direction and a citable source (path + line span + evidence)."""
    from server.index import relstore

    ws = "/fake/ws-rel2"
    store.replace_file(
        ws,
        "cv.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 5,
                "text": "Ada founded Acme",
                "vec": _unit(1),
                "entities": [_ent("Ada", "person"), _ent("Acme", "organization")],
            }
        ],
    )
    relstore.set_file_relations_v2(
        ws,
        "cv.md",
        [
            {
                "source": "Ada",
                "target": "Acme",
                "predicate": "works_at",
                "strength": 5,
                "evidence": "Ada works at Acme",
                "start_line": 2,
                "end_line": 2,
            }
        ],
    )
    facts = relstore.facts_for_entity(ws, "Ada")
    assert len(facts) == 1
    f = facts[0]
    assert f["predicate"] == "works_at" and f["other"] == "Acme" and f["direction"] == "out"
    assert f["score"] == 1.0  # strength 5 -> certain
    assert f["cite"]["path"] == "cv.md" and f["cite"]["start_line"] == 2
    # The same fact is visible from the target as an incoming relation.
    back = relstore.facts_for_entity(ws, "Acme")
    assert back and back[0]["direction"] == "in" and back[0]["other"] == "Ada"


def test_relations2_same_fact_in_two_files_survives_reindex_of_one():
    """Two files asserting the same fact (identical evidence) each keep their own
    row (keyed by path), so re-indexing/clearing ONE file doesn't orphan the fact
    the other still states — cross-file dedup happens at read time."""
    from server.index import relstore

    ws = "/fake/ws-rel2-dup"
    for f in ("a.md", "b.md"):
        store.replace_file(
            ws,
            f,
            {"hash": "h", "size": 1, "mtime": 1.0},
            [
                {
                    "start_line": 1,
                    "end_line": 1,
                    "text": "Alice works at Acme.",
                    "vec": _unit(1),
                    "entities": [_ent("Alice", "person"), _ent("Acme", "organization")],
                }
            ],
        )
        relstore.set_file_relations_v2(
            ws,
            f,
            [
                {
                    "source": "Alice",
                    "target": "Acme",
                    "predicate": "works_at",
                    "strength": 5,
                    "evidence": "Alice works at Acme.",
                }
            ],
        )
    facts = relstore.facts_for_entity(ws, "Alice")
    assert len(facts) == 1 and facts[0]["sources"] == 2  # both files counted, deduped at read
    # Clear a.md's relations (simulates editing/removing the sentence in a.md).
    relstore.set_file_relations_v2(ws, "a.md", [])
    still = relstore.facts_for_entity(ws, "Alice")
    assert len(still) == 1 and still[0]["other"] == "Acme"  # b.md still asserts it — not orphaned
    assert still[0]["sources"] == 1


def test_evidence_line_finds_cooccurrence():
    """evidence_line returns the 1-based line where both entities co-occur (over the
    same text the chunker uses), else the first line naming the source."""
    from server.index.relstore import evidence_line

    text = "Intro line.\nAda works at Acme as founder.\nUnrelated tail.\n"
    assert evidence_line(text, "Ada", "Acme") == (2, 2, "Ada works at Acme as founder.")
    # No co-occurrence: fall back to the first line naming the source.
    text2 = "Alpha mentions Ada only.\nBeta mentions Acme only.\n"
    assert evidence_line(text2, "Ada", "Acme")[0] == 1
    assert evidence_line("", "a", "b") == (None, None, None)


def test_relations2_repointed_on_entity_merge(monkeypatch):
    """When entity dedup merges two variants, their node-id-keyed relations are
    repointed onto the canonical node instead of dangling."""
    monkeypatch.setattr(store, "ENTITY_SEMANTIC_MERGE", False)  # lexical merge only, no embedder
    from server.index import relstore

    ws = "/fake/ws-rel2-merge"
    for path, name in (("a.md", "Acme Corp"), ("b.md", "acme corp")):
        store.replace_file(
            ws,
            path,
            {"hash": "h", "size": 1, "mtime": 1.0},
            [
                {
                    "start_line": 1,
                    "end_line": 1,
                    "text": path,
                    "vec": _unit(1),
                    "entities": [_ent(name, "organization"), _ent("Bob", "person")],
                }
            ],
        )
    relstore.set_file_relations_v2(
        ws,
        "b.md",
        [{"source": "acme corp", "target": "Bob", "predicate": "works_at", "strength": 4}],
    )
    assert relstore.facts_for_entity(ws, "acme corp")  # exists before the merge
    assert store.dedupe_entities(ws) >= 1  # the two Acme variants collapse

    # Seen from Bob (unchanged), the fact still resolves — repointed, not dangling.
    bob_facts = relstore.facts_for_entity(ws, "Bob")
    assert any(
        f["predicate"] == "works_at" and f["direction"] == "in" and "cme" in f["other"].lower()
        for f in bob_facts
    )


def test_workspace_graph_query_tool_cites_sources(monkeypatch):
    """The workspace_graph_query executor returns an entity's typed facts with
    #wsfile citations carrying the line anchor."""
    from server.index import graph_tool, relstore

    ws = "/fake/ws-gtool"
    store.replace_file(
        ws,
        "cv.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 5,
                "text": "x",
                "vec": _unit(1),
                "entities": [_ent("Ada", "person"), _ent("Acme", "organization")],
            }
        ],
    )
    relstore.set_file_relations_v2(
        ws,
        "cv.md",
        [
            {
                "source": "Ada",
                "target": "Acme",
                "predicate": "works_at",
                "strength": 5,
                "start_line": 2,
                "end_line": 2,
            }
        ],
    )
    monkeypatch.setattr(graph_tool, "get_workspace_path", lambda: ws)
    monkeypatch.setattr(graph_tool.paths, "is_indexed", lambda p: True)
    out = graph_tool.exec_workspace_graph_query({"entity": "Ada"}, [], None)
    assert "works_at" in out and "Acme" in out
    assert "#wsfile=" in out and "&L=2-2" in out


def test_entity_descriptions_persist_and_surface(monkeypatch):
    """set_node_descriptions stores a one-line description on the canonical node;
    entity_graph surfaces it on the centre node (the entity-pivot view)."""
    monkeypatch.setattr(store, "ENTITY_SEMANTIC_MERGE", False)
    ws = "/fake/ws-desc"
    store.replace_file(
        ws,
        "a.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "Ada built the engine",
                "vec": _unit(1),
                "entities": [{"name": "Ada Lovelace", "label": "person"}],
            }
        ],
    )
    assert (
        store.set_node_descriptions(
            ws, [{"name": "Ada Lovelace", "label": "person", "description": "A mathematician."}]
        )
        == 1
    )
    centre = store.entity_graph(ws, "ada lovelace")["nodes"][0]
    assert centre["type"] == "entity" and centre["description"] == "A mathematician."


def test_scored_relation_surfaces_on_edge():
    """A relation's score persists and surfaces on the entity-pivot typed edge."""
    ws = "/fake/ws-relscore"
    for f, nm, lbl in [("a.md", "Acme", "organization"), ("b.md", "Bob", "person")]:
        store.replace_file(
            ws,
            f,
            {"hash": "h", "size": 1, "mtime": 1.0},
            [
                {
                    "start_line": 1,
                    "end_line": 1,
                    "text": "x",
                    "vec": _unit(1),
                    "entities": [{"name": nm, "label": lbl}],
                }
            ],
        )
    store.set_file_relations(ws, "a.md", [("Acme", "Bob", "employs", 4.0)])
    edges = [e for e in store.entity_graph(ws, "acme")["edges"] if e.get("relation") == "employs"]
    assert edges and edges[0]["score"] == 4.0


def test_entity_graph_includes_typed_relations():
    """A typed relation surfaces the other endpoint as an entity node plus a
    labeled entity↔entity edge in the centred graph."""
    ws = "/fake/ws-rel"
    ents = [{"name": "Acme", "label": "organization"}, {"name": "Bob", "label": "person"}]
    store.replace_file(
        ws,
        "a.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "Bob works at Acme",
                "vec": _unit(1),
                "entities": ents,
            }
        ],
    )
    store.set_file_relations(ws, "a.md", [("Bob", "Acme", "works_at")])
    g = store.entity_graph(ws, "bob")
    ent_nodes = {n["name"] for n in g["nodes"] if n.get("type") == "entity"}
    assert {"Bob", "Acme"} <= ent_nodes
    assert any(e.get("relation") == "works_at" for e in g["edges"])


def test_file_relations_replaced_and_deleted():
    """Relations are scoped to a file: re-indexing clears the file's old rows
    and deleting the file drops them entirely."""
    import sqlite3 as _sql

    ws = "/fake/ws-rel2"
    ents = [{"name": "A", "label": "x"}, {"name": "B", "label": "x"}]
    store.replace_file(
        ws,
        "f.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [{"start_line": 1, "end_line": 1, "text": "t", "vec": _unit(1), "entities": ents}],
    )
    store.set_file_relations(ws, "f.md", [("A", "B", "rel1")])

    def relcount():
        c = _sql.connect(store.db_path(ws))
        try:
            return c.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        finally:
            c.close()

    assert relcount() == 1
    store.replace_file(
        ws,
        "f.md",
        {"hash": "h2", "size": 1, "mtime": 2.0},  # re-index clears old rows
        [{"start_line": 1, "end_line": 1, "text": "t2", "vec": _unit(2), "entities": ents}],
    )
    assert relcount() == 0
    store.set_file_relations(ws, "f.md", [("A", "B", "rel1"), ("B", "A", "rel2")])
    assert relcount() == 2
    store.delete_file(ws, "f.md")  # deleting the file drops them
    assert relcount() == 0


def test_ws_settings_defaults_persist_and_validate():
    """Per-workspace settings: conservative defaults, validated on write,
    persisted in the index meta, and independent per folder."""
    from server.index import wssettings

    ws = "/fake/ws-settings"
    store.replace_file(
        ws,
        "a.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [{"start_line": 1, "end_line": 1, "text": "x", "vec": _unit(1), "entities": []}],
    )
    s = wssettings.get_settings(ws)
    assert s["schedule"]["enabled"] is False and s["schedule"]["frequency"] == "daily"
    assert s["typed_relations"] == {"enabled": False, "engine": "none"}
    assert s["refresh_when_closed"] is False

    s = wssettings.update_settings(
        ws,
        {
            "schedule": {"enabled": True, "frequency": "every_n_days", "interval_days": 99},
            "typed_relations": {"enabled": True, "engine": "bogus"},
            "refresh_when_closed": True,
        },
    )
    assert s["schedule"]["enabled"] is True and s["schedule"]["interval_days"] == 30  # clamped
    assert (
        s["typed_relations"]["enabled"] is True and s["typed_relations"]["engine"] == "none"
    )  # bad→none
    assert s["refresh_when_closed"] is True
    assert wssettings.get_settings(ws)["schedule"]["frequency"] == "every_n_days"  # persisted

    # The relation engine is independent of the entity model: "gliner2" (native,
    # LLM-free) is a valid relation engine and is accepted on its own.
    s = wssettings.update_settings(ws, {"typed_relations": {"engine": "gliner2"}})
    assert s["typed_relations"]["engine"] == "gliner2"

    # A different folder is unaffected (settings are per-workspace).
    store.replace_file(
        "/fake/ws-other",
        "b.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [{"start_line": 1, "end_line": 1, "text": "y", "vec": _unit(2), "entities": []}],
    )
    assert wssettings.get_settings("/fake/ws-other")["schedule"]["enabled"] is False


def test_ws_settings_pending_before_index_then_promoted(monkeypatch, tmp_path):
    """Configure a folder BEFORE it's indexed: settings persist to the pending
    store (no index DB created), then the first index promotes them into meta —
    so e.g. relationships are captured on the first pass."""
    import os

    from server.index import wssettings

    monkeypatch.setattr(wssettings, "_PENDING_PATH", str(tmp_path / "pending.json"))
    ws = "/fake/ws-preindex"

    s = wssettings.update_settings(ws, {"typed_relations": {"enabled": True}})
    assert s["typed_relations"]["enabled"] is True
    assert not os.path.exists(store.db_path(ws))  # no spurious index
    assert wssettings.get_settings(ws)["typed_relations"]["enabled"] is True  # served from pending

    store.replace_file(
        ws,
        "a.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [{"start_line": 1, "end_line": 1, "text": "x", "vec": _unit(1), "entities": []}],
    )
    assert wssettings.get_settings(ws)["typed_relations"]["enabled"] is True  # promoted into meta
    assert wssettings._pending_get(ws) is None  # pending cleared


def test_ws_settings_no_db_created_for_unindexed():
    """Reading settings for a folder that was never indexed returns defaults and
    must NOT create an index DB as a side effect (store._connect always would)."""
    import os

    from server.index import wssettings

    ws = "/fake/ws-never-indexed"
    s = wssettings.get_settings(ws)
    assert s["schedule"]["enabled"] is False
    assert not os.path.exists(store.db_path(ws))  # no spurious index


def test_relations_skips_when_no_engine_chosen():
    """engine 'none' extracts nothing (no cloud call, no model load)."""
    from server.index import relations

    assert relations.extract_relations("Bob works at Acme", ["Bob", "Acme"], "none") == []


def test_relations_routes_to_local_gemma_engine(monkeypatch):
    """engine='local' routes extraction through the on-device model
    (runtime.complete on local_gemma); its JSON is parsed/validated as usual."""
    import server.local.runtime as rt
    from server.index import relations

    monkeypatch.setattr(rt, "is_downloaded", lambda key: True)
    seen = {}

    def fake_complete(key, system, user, max_tokens=1500):
        seen["key"] = key
        return '[{"source":"Bob","target":"Acme","type":"works_at"}]'

    monkeypatch.setattr(rt, "complete", fake_complete)
    rels = relations.extract_relations("Bob works at Acme", ["Bob", "Acme"], "local")
    assert rels == [("Bob", "Acme", "works_at", 3.0)]  # 4-tuple: includes 1–5 score
    assert seen["key"] == "local_gemma"  # the general Gemma model


def test_relations_local_engine_skips_when_model_missing(monkeypatch):
    """If the local model isn't downloaded, the local engine returns [] (never
    silently falls back to the cloud, and never blocks indexing)."""
    import server.local.runtime as rt
    from server.index import relations

    monkeypatch.setattr(rt, "is_downloaded", lambda key: False)

    def boom(*a, **k):
        raise AssertionError("complete() must not be called when the model is missing")

    monkeypatch.setattr(rt, "complete", boom)
    assert relations.extract_relations("Bob works at Acme", ["Bob", "Acme"], "local") == []


def test_apply_workspace_installs_per_folder_trigger(monkeypatch):
    """scheduler.apply_workspace installs one job for the folder's own cadence
    (passing the folder as the job arg), and nothing when its schedule is off."""
    from server.index import scheduler, wssettings

    class FakeSched:
        def __init__(self):
            self.jobs = []

        def remove_job(self, _id):
            pass

        def add_job(self, fn, trigger, **kw):
            self.jobs.append((trigger, kw))

    fake = FakeSched()
    monkeypatch.setattr(scheduler, "_scheduler", fake)
    monkeypatch.setattr(
        wssettings,
        "get_settings",
        lambda ws: {
            "schedule": {"enabled": True, "frequency": "weekly", "weekday": "wed", "hour": 9}
        },
    )
    scheduler.apply_workspace("/fake/ws")
    trig, kw = fake.jobs[-1]
    assert (
        trig == "cron"
        and kw["day_of_week"] == "wed"
        and kw["hour"] == 9
        and kw["args"] == ["/fake/ws"]
    )

    monkeypatch.setattr(wssettings, "get_settings", lambda ws: {"schedule": {"enabled": False}})
    n = len(fake.jobs)
    scheduler.apply_workspace("/fake/ws")
    assert len(fake.jobs) == n  # disabled → no job


def test_file_graph_nodes_tagged_as_files():
    ws = "/fake/ws-typed"
    ent = [{"name": "PaymentService", "label": "service"}]
    store.replace_file(
        ws,
        "a.py",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "PaymentService",
                "vec": _unit(1),
                "entities": ent,
            }
        ],
    )
    g = store.file_graph(ws)
    assert g["nodes"] and all(n.get("type") == "file" for n in g["nodes"])


def test_all_workspaces_graph_links_across_workspaces():
    """The unified graph merges every indexed workspace, groups nodes by source
    workspace, and links files that share entities — flagging cross-workspace
    edges and surfacing the shared entity names."""
    ent = [{"name": "PaymentService", "label": "service"}]
    wsA, wsB = "/fake/all-a", "/fake/all-b"
    store.replace_file(
        wsA,
        "a.py",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "uses PaymentService",
                "vec": _unit(1),
                "entities": ent,
            }
        ],
    )
    store.set_meta(wsA, workspace=wsA)
    store.replace_file(
        wsB,
        "b.py",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "also PaymentService",
                "vec": _unit(2),
                "entities": ent,
            }
        ],
    )
    store.set_meta(wsB, workspace=wsB)

    g = store.all_workspaces_graph()
    ids = {n["id"] for n in g["nodes"]}
    assert any(i.endswith("/a.py") for i in ids) and any(i.endswith("/b.py") for i in ids)
    assert len({n["group"] for n in g["nodes"]}) == 2  # one colour group per workspace
    cross = [e for e in g["edges"] if e["cross"]]
    assert (
        cross and "PaymentService" in cross[0]["entities"]
    )  # linked across folders by the shared entity
    assert {w["name"] for w in g["workspaces"]} == {"all-a", "all-b"}


def test_retrieve_grounding_includes_graph_hop_and_full_chunks(monkeypatch):
    """Grounding returns near-full-chunk excerpts (not the old 240-char snippet),
    folds in the GraphRAG hop (chunks linked by shared entities), and steers the
    model to answer from the index rather than re-reading the files."""
    from server.index import embedder

    ws = "/fake/ws-ground"
    ent = [{"name": "PaymentService", "label": "service"}]
    filler = "PaymentService authorizes the card payment. " * 10  # ~440 chars, past the old 240 cap
    long_text = filler + "ZZbeyond240marker settlement is recorded here."
    store.replace_file(
        ws,
        "a.py",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [{"start_line": 1, "end_line": 60, "text": long_text, "vec": _unit(1), "entities": ent}],
    )
    store.replace_file(
        ws,
        "b.py",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 5,
                "text": "b also mentions PaymentService",
                "vec": _unit(2),
                "entities": ent,
            }
        ],
    )
    monkeypatch.setattr(embedder, "embed_query", lambda q: _unit(1))  # ranks a.py first

    # k=1 so only a.py is a vector match; b.py must come in via the entity hop.
    g = pipeline.retrieve_grounding([ws], "how is the card payment authorized", k=1)

    # (3) steering: treat the index as the answer, don't crawl files
    assert "source of truth" in g and "do not re-read files" in g
    # (1) full chunk included — a marker ~440 chars in would be lost under the old 240-char cap
    assert "ZZbeyond240marker" in g
    assert "a.py:1-60" in g
    # (2) graph hop folded in as supporting context
    assert "Related passages (linked through shared entities)" in g
    assert "b.py:1-5" in g
    # citation links carry the line-anchor param and an absolute href
    import re as _re

    assert _re.search(r"\]\(#wsfile=[^)]+&L=\d+-\d+\)", g)


def test_build_context_query_folds_recent_turns():
    """Tier 1: a follow-up carries the prior turn's intent, with the current
    question last (the dominant signal); no history leaves it unchanged."""
    hist = [
        {"role": "user", "content": "what were her jobs and durations?"},
        {"role": "assistant", "content": "Paulina was a Data Engineer."},
    ]
    q = pipeline.build_context_query("what about ada?", hist)
    assert "jobs and durations" in q  # prior intent folded in
    assert q.strip().endswith("what about ada?")  # current question weighted last
    assert pipeline.build_context_query("hello", []) == "hello"
    assert pipeline.build_context_query("hello", None) == "hello"

    # List-shaped content (tool_use/tool_result blocks) must not crash; only the
    # text blocks contribute, and a tool-only turn is simply skipped.
    list_hist = [
        {"role": "user", "content": [{"type": "text", "text": "summarize the contract"}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "ws_read_file", "input": {}}],
        },
    ]
    q2 = pipeline.build_context_query("and the payment terms?", list_hist)
    assert "summarize the contract" in q2 and q2.strip().endswith("and the payment terms?")


def test_rrf_fuse_rewards_cross_query_agreement():
    """Tier 2: reciprocal-rank fusion ranks a chunk that both queries surface
    above one only a single query found, regardless of raw cosine scale."""
    a = {"_abs": "/x/a", "start_line": 1, "end_line": 2, "text": "a", "score": 0.95}
    b = {"_abs": "/x/b", "start_line": 1, "end_line": 2, "text": "b", "score": 0.50}
    c = {"_abs": "/x/c", "start_line": 1, "end_line": 2, "text": "c", "score": 0.80}
    fused = pipeline._rrf_fuse([[a, b], [b, c]])  # b appears in both lists
    assert fused[0]["_abs"] == "/x/b"  # agreement wins
    assert next(m for m in fused if m["_abs"] == "/x/b")["score"] == 0.50  # keeps best cosine


def test_retrieve_grounding_auto_merges_small_doc_but_not_book(monkeypatch):
    """Auto-merge (parent-document retrieval): a small focused doc with a chunk
    in the top matches is spliced in FULL (so a CV's whole history grounds, not
    just its header), each chunk exactly once; a large doc (book) is never
    whole-merged so it can't take over the answer."""
    from server.index import embedder

    ws = "/fake/ws-automerge"
    monkeypatch.setattr(embedder, "embed_query", lambda q: _unit(1))
    # cv.md: 5 chunks. Two match the query (_unit(1)); three are off-query
    # (_unit(2) ~ orthogonal -> below the score floor, so they only arrive via
    # auto-merge, which is the whole point).
    cv = [
        {
            "start_line": 1,
            "end_line": 5,
            "text": "cv header current role",
            "vec": _unit(1),
            "entities": [],
        },
        {
            "start_line": 6,
            "end_line": 10,
            "text": "role A 2020 to 2021",
            "vec": _unit(2),
            "entities": [],
        },
        {
            "start_line": 11,
            "end_line": 15,
            "text": "role B 2018 to 2020",
            "vec": _unit(2),
            "entities": [],
        },
        {
            "start_line": 16,
            "end_line": 20,
            "text": "role C 2016 to 2018",
            "vec": _unit(2),
            "entities": [],
        },
        {
            "start_line": 21,
            "end_line": 25,
            "text": "education section",
            "vec": _unit(1),
            "entities": [],
        },
    ]
    store.replace_file(ws, "cv.md", {"hash": "h", "size": 1, "mtime": 1.0}, cv)
    # book.md: 13 chunks (> _AUTOMERGE_MAX_DOC_CHUNKS=12); page 0 matches.
    book = [
        {
            "start_line": i * 5 + 1,
            "end_line": i * 5 + 5,
            "text": f"book page {i}",
            "vec": _unit(1) if i == 0 else _unit(3),
            "entities": [],
        }
        for i in range(13)
    ]
    store.replace_file(ws, "book.md", {"hash": "h", "size": 1, "mtime": 1.0}, book)

    g = pipeline.retrieve_grounding([ws], "tell me about the cv", k=12)

    # Small doc fully merged — all five line-ranges present exactly once (no dup).
    for lr in ("cv.md:1-5", "cv.md:6-10", "cv.md:11-15", "cv.md:16-20", "cv.md:21-25"):
        assert g.count(lr) == 1, f"{lr} should appear exactly once"
    # Book gated: its off-query pages are NOT whole-merged into the answer.
    assert "book.md:6-10" not in g and "book.md:31-35" not in g


def test_fts_keyword_surfaces_filename_match(monkeypatch):
    """Hybrid retrieval: a file whose CONTENT doesn't match the query but whose
    FILENAME does (e.g. buddy.json holding name "Prickle") is surfaced by the
    BM25 keyword leg and survives the cosine floor, so the answer grounds even
    though the dense similarity is ~0."""
    from server.index import embedder

    ws = "/fake/ws-kw"
    # Query embedding is orthogonal to everything -> all dense cosines ~0 (below
    # the floor); only the keyword leg can surface a match.
    monkeypatch.setattr(embedder, "embed_query", lambda q: _unit(99))
    store.replace_file(
        ws,
        "buddy.json",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 4,
                "text": '{"name": "Prickle", "personality": "a cactus"}',
                "vec": _unit(1),
                "entities": [],
            }
        ],
    )
    store.replace_file(
        ws,
        "notes.txt",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 2,
                "text": "unrelated content",
                "vec": _unit(2),
                "entities": [],
            }
        ],
    )
    g = pipeline.retrieve_grounding([ws], "what was buddy name?")
    assert "buddy.json:1-4" in g  # surfaced by the filename keyword hit
    assert "Prickle" in g  # so the answer is in the grounding


def test_fts_search_filters_stopwords():
    """The keyword leg ignores stopword-only queries (so a chunk sharing only
    'the'/'is'/'how' isn't a spurious keyword hit) but matches content terms."""
    ws = "/fake/ws-stop"
    store.replace_file(
        ws,
        "doc.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 1,
                "text": "the report is about Prickle",
                "vec": _unit(1),
                "entities": [],
            }
        ],
    )
    assert store.fts_search(ws, "what is the how", k=5) == []  # all stopwords
    hits = store.fts_search(ws, "Prickle", k=5)
    assert hits and hits[0]["path"] == "doc.md" and "_bm25" in hits[0]


def test_chunk_contexts_modes(monkeypatch):
    """The per-chunk context header by mode: off → none, filename → the file
    path (so a filename token enters the embedding), llm → routed to the LLM
    contextualizer."""
    from server.index import contextualize

    chunks = [{"text": "alpha"}, {"text": "beta"}]
    assert pipeline._chunk_contexts("/x/Career", "cv.pdf", "doc", chunks, "off", "local") == [
        "",
        "",
    ]
    fn = pipeline._chunk_contexts("/x/Career", "cv.pdf", "doc", chunks, "filename", "local")
    assert fn == ["File: Career/cv.pdf", "File: Career/cv.pdf"]
    monkeypatch.setattr(
        contextualize,
        "contextualize_chunks",
        lambda fname, doc, texts, engine: [f"ctx:{t}" for t in texts],
    )
    llm = pipeline._chunk_contexts("/x/Career", "cv.pdf", "doc", chunks, "llm", "haiku")
    assert llm == ["ctx:alpha", "ctx:beta"]


def test_build_reembeds_on_context_mode_change(tmp_path, monkeypatch):
    """A change to chunk_context.mode force-re-embeds even unchanged files and
    stamps context_mode; an unchanged same-mode build still skips. Heavy models
    are stubbed so this exercises only the build gate logic."""
    import numpy as np

    from server.index import embedder, extractor, wssettings

    monkeypatch.setattr(
        embedder,
        "embed_documents",
        lambda texts: np.stack([_unit(1) for _ in texts])
        if texts
        else np.zeros((0, EMBED_DIM), np.float32),
    )
    monkeypatch.setattr(extractor, "extract_entities", lambda t, *a, **k: [])
    monkeypatch.setattr(embedder, "unload", lambda: None)
    monkeypatch.setattr(extractor, "unload", lambda: None)
    (tmp_path / "a.txt").write_text("hello world, some indexable content here")
    d = str(tmp_path)

    wssettings.update_settings(d, {"chunk_context": {"mode": "off"}})
    assert pipeline.build(d)["changed_files"] == 1
    assert store.get_meta(d)["context_mode"] == "off"
    assert pipeline.build(d)["changed_files"] == 0  # unchanged + same mode → skip
    wssettings.update_settings(d, {"chunk_context": {"mode": "filename"}})
    assert pipeline.build(d)["changed_files"] == 1  # mode change → force re-embed
    assert store.get_meta(d)["context_mode"] == "filename"


def test_retrieve_grounding_applies_reranker(monkeypatch):
    """When rag_reranker is on, the fused candidates are reordered by the
    reranker's scores before the top-k cut (here: promote 'chunk 3' to #1)."""
    import server.infrastructure.feature_flags as ff
    from server.index import embedder, reranker

    ws = "/fake/ws-rerank"
    monkeypatch.setattr(embedder, "embed_query", lambda q: _unit(1))
    recs = [
        {"start_line": i, "end_line": i, "text": f"chunk {i}", "vec": _unit(1), "entities": []}
        for i in (1, 2, 3)
    ]
    store.replace_file(ws, "doc.md", {"hash": "h", "size": 1, "mtime": 1.0}, recs)
    monkeypatch.setattr(ff, "is_enabled", lambda name: name == "rag_reranker")
    monkeypatch.setattr(
        reranker,
        "rerank",
        lambda query, passages: [1.0 if "chunk 3" in p else 0.1 for p in passages],
    )
    g = pipeline.retrieve_grounding([ws], "anything", k=12)
    passage_lines = [ln for ln in g.splitlines() if "doc.md:" in ln]
    assert passage_lines and "doc.md:3-3" in passage_lines[0]  # reranker put chunk 3 first


def test_store_stats_and_manifest():
    ws = "/fake/ws-stats"
    store.replace_file(
        ws,
        "a.py",
        {"hash": "h1", "size": 10, "mtime": 2.0},
        [{"start_line": 1, "end_line": 1, "text": "x", "vec": _unit(1), "entities": []}],
    )
    assert store.get_manifest(ws)["a.py"]["hash"] == "h1"
    assert store.stats(ws)["files"] == 1


# ── pipeline: incremental change detection (embedder/GLiNER stubbed) ─────────


@pytest.fixture
def _stub_models(monkeypatch):
    from server.index import embedder, extractor

    monkeypatch.setattr(
        embedder, "embed_documents", lambda texts: np.vstack([_unit(hash(t) % 9999) for t in texts])
    )
    monkeypatch.setattr(extractor, "extract_entities", lambda text, *a, **k: [])
    monkeypatch.setattr(embedder, "unload", lambda: None)
    monkeypatch.setattr(extractor, "unload", lambda: None)


def test_build_incremental(tmp_path, _stub_models):
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "one.py").write_text("def one():\n    return 1\n")
    (ws / "two.md").write_text("# Title\nsome docs here\n")
    s = pipeline.build(str(ws))
    assert s["files"] == 2 and s["changed_files"] == 2

    # No changes → nothing re-embedded.
    s = pipeline.build(str(ws))
    assert s["changed_files"] == 0 and s["files"] == 2

    # Edit one file → exactly one re-embed.
    (ws / "one.py").write_text("def one():\n    return 42  # changed\n")
    s = pipeline.build(str(ws))
    assert s["changed_files"] == 1 and s["files"] == 2

    # Delete a file → removed, manifest shrinks.
    (ws / "two.md").unlink()
    s = pipeline.build(str(ws))
    assert s["removed_files"] == 1 and s["files"] == 1


def test_data_files_are_not_entity_mined(tmp_path, monkeypatch):
    """Structured-data files (.json/.yaml/…) are embedded and searchable but never
    entity-mined, so their keys never become graph nodes; other files still are."""
    from server.index import embedder, extractor

    monkeypatch.setattr(
        embedder, "embed_documents", lambda texts: np.vstack([_unit(hash(t) % 9999) for t in texts])
    )
    monkeypatch.setattr(embedder, "unload", lambda: None)
    monkeypatch.setattr(extractor, "unload", lambda: None)
    # Any text yields one entity, so nodes appear ONLY where NER actually runs.
    monkeypatch.setattr(
        extractor,
        "extract_entities",
        lambda t, *a, **k: [{"name": "Widget", "label": "product", "score": 0.9}],
    )
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "config.json").write_text('{"name": "x", "settings": {"personality": "cactus"}}\n')
    (ws / "notes.md").write_text("# Notes\nWidget is great.\n")
    pipeline.build(str(ws))

    import sqlite3 as _sql

    conn = _sql.connect(store.db_path(str(ws)))
    json_ids = [c["chunk_id"] for c in store.chunks_for_file(str(ws), "config.json")]
    assert json_ids  # the JSON was chunked + embedded (still searchable)
    q = ",".join("?" * len(json_ids))
    assert (
        conn.execute(
            f"SELECT COUNT(*) FROM node_chunks WHERE chunk_id IN ({q})", json_ids
        ).fetchone()[0]
        == 0
    )  # ...but produced no entities
    md_ids = [c["chunk_id"] for c in store.chunks_for_file(str(ws), "notes.md")]
    q2 = ",".join("?" * len(md_ids))
    assert (
        conn.execute(
            f"SELECT COUNT(*) FROM node_chunks WHERE chunk_id IN ({q2})", md_ids
        ).fetchone()[0]
        >= 1
    )  # the markdown file still got its entity
    conn.close()


def test_remove_index(tmp_path, _stub_models):
    ws = tmp_path / "proj2"
    ws.mkdir()
    (ws / "f.py").write_text("print('hi')\n")
    pipeline.build(str(ws))
    assert paths.is_indexed(str(ws))
    store.remove_index(str(ws))
    assert not paths.is_indexed(str(ws))


# ── rich-file / image extraction routing (extract layer stubbed) ─────────────


def test_extract_text_routes_by_type(tmp_path, monkeypatch):
    import server.extract as extract
    import server.extract.media as media_mod

    monkeypatch.setattr(extract, "convert_document", lambda raw, ext, name: f"DOC:{name}")
    monkeypatch.setattr(extract, "ocr_image_bytes", lambda raw: "IMG-OCR-TEXT")
    # extract_media returns (text, frames); the index pipeline keeps only text.
    monkeypatch.setattr(
        media_mod, "extract_media", lambda raw, ext, name: (f"TRANSCRIPT:{name}", [])
    )

    t = tmp_path / "a.py"
    t.write_text("print('x')\n")
    assert pipeline._extract_text(str(t), ".py", "a.py").startswith("print")  # text: read directly

    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    assert (
        pipeline._extract_text(str(p), ".pdf", "doc.pdf") == "DOC:doc.pdf"
    )  # rich: convert_document

    img = tmp_path / "scan.png"
    img.write_bytes(b"\x89PNG fake")
    assert pipeline._extract_text(str(img), ".png", "scan.png") == "IMG-OCR-TEXT"  # image: OCR

    small_csv = tmp_path / "s.csv"
    small_csv.write_text("a,b\n1,2\n")
    assert (
        pipeline._extract_text(str(small_csv), ".csv", "s.csv").strip() == "a,b\n1,2"
    )  # small sheet → fast read

    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"\x00\x00fakevideo")
    assert (
        pipeline._extract_text(str(vid), ".mp4", "clip.mp4") == "TRANSCRIPT:clip.mp4"
    )  # media → transcript


def test_build_indexes_rich_and_image_files(tmp_path, monkeypatch, _stub_models):
    import server.extract as extract

    monkeypatch.setattr(
        extract, "convert_document", lambda raw, ext, name: f"converted {name} body text"
    )
    monkeypatch.setattr(extract, "ocr_image_bytes", lambda raw: "ocr extracted text from the image")
    ws = tmp_path / "mixed"
    ws.mkdir()
    (ws / "code.py").write_text("def f():\n    return 1\n")
    (ws / "resume.pdf").write_bytes(b"%PDF fake bytes")
    (ws / "scan.png").write_bytes(b"PNG fake bytes")
    s = pipeline.build(str(ws))
    assert s["files"] == 3  # text + pdf + image all indexed, not just the .py


def test_tool_ref_link_format():
    from server.index.tool import _ref

    # Absolute href (resolves regardless of connected workspace) with the cited
    # line range; display text stays workspace-relative. Space -> %20.
    assert (
        _ref("04_Career/My CV.pdf", 1, 5, "/ws")
        == "[04_Career/My CV.pdf:1-5](#wsfile=/ws/04_Career/My%20CV.pdf&L=1-5)"
    )
    # Legacy call without a workspace root keeps a relative href (still parseable).
    assert (
        _ref("04_Career/My CV.pdf", 1, 5)
        == "[04_Career/My CV.pdf:1-5](#wsfile=04_Career/My%20CV.pdf&L=1-5)"
    )


def test_citation_link_encoding():
    from server.index.citations import citation_link

    # Ampersand and colon in the path are percent-encoded, so the first raw '&'
    # is unambiguously the params boundary.
    assert (
        citation_link("a & b:1.md", 3, 9, "/w/a & b:1.md")
        == "[a & b:1.md:3-9](#wsfile=/w/a%20%26%20b%3A1.md&L=3-9)"
    )
    # Polish diacritics in the path round-trip through quote (ó -> %C3%B3, ł -> %C5%82).
    assert "Sp%C3%B3%C5%82ka%20Umowa.pdf&L=7-7)" in citation_link(
        "Umowa.pdf", 7, 7, "/w/Spółka Umowa.pdf"
    )


def test_textless_file_recorded_then_skipped(tmp_path, monkeypatch, _stub_models):
    """A file that extracts to nothing (failed OCR) must be recorded with 0 chunks
    so the next build skips it instead of re-OCR'ing it forever."""
    import server.extract as extract

    calls = {"n": 0}

    def fake_convert(raw, ext, name):
        calls["n"] += 1
        return ""  # nothing extracted (e.g. a scan OCR failed)

    monkeypatch.setattr(extract, "convert_document", fake_convert)
    ws = tmp_path / "p"
    ws.mkdir()
    (ws / "scan.pdf").write_bytes(b"%PDF empty")

    s1 = pipeline.build(str(ws))
    assert calls["n"] == 1
    assert s1["files"] == 1 and s1["chunks"] == 0  # recorded, zero chunks
    assert store.get_manifest(str(ws)).get("scan.pdf") is not None

    s2 = pipeline.build(str(ws))
    assert calls["n"] == 1  # NOT re-extracted (skipped via gate)
    assert s2["files"] == 1


def test_no_text_recognized_sentinel_filtered(tmp_path, monkeypatch):
    import server.extract as extract

    monkeypatch.setattr(extract, "convert_document", lambda raw, ext, name: "[No text recognized]")
    p = tmp_path / "scan.pdf"
    p.write_bytes(b"%PDF")
    assert pipeline._extract_text(str(p), ".pdf", "scan.pdf") is None


def test_large_csv_streamed_sample(tmp_path):
    """A >1 MB CSV is sampled (header + 20 rows + total count), not read whole."""
    p = tmp_path / "big.csv"
    with open(p, "w") as f:
        f.write("name,role,city\n")
        for i in range(80_000):
            f.write(f"person{i},engineer,city{i % 50}\n")
    assert p.stat().st_size > 1_000_000
    out = pipeline._extract_text(str(p), ".csv", "big.csv")
    assert out.startswith("[Large CSV: 80000 data rows x 3 columns; showing the first 20.]")
    assert "| name | role | city |" in out
    assert out.count("\n") < 40  # bounded, not 80k rows


def test_small_csv_fast_path(tmp_path):
    p = tmp_path / "small.csv"
    p.write_text("a,b\n1,2\n3,4\n")
    out = pipeline._extract_text(str(p), ".csv", "small.csv")
    assert out.strip() == "a,b\n1,2\n3,4"  # direct read, no conversion


def test_sample_large_sheet_xlsx(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["id", "val"])
    for i in range(100):
        ws.append([i, i * 2])
    p = tmp_path / "s.xlsx"
    wb.save(str(p))
    out = pipeline._sample_large_sheet(str(p), ".xlsx")
    assert "## Sheet1" in out and "| id | val |" in out
    assert out.count("\n") < 60  # bounded sample, not 100 rows


def test_build_cancel_keeps_partial(tmp_path, _stub_models):
    """Requesting cancel mid-build stops after the current file, keeps what was
    indexed, and does not stamp last_indexed_at (the index is partial)."""
    ws = tmp_path / "big"
    ws.mkdir()
    for i in range(5):
        (ws / f"f{i}.py").write_text(f"def f{i}():\n    return {i}\n")

    seen = []

    def prog(done, total, rel):
        seen.append(rel)
        if len(seen) == 1:
            pipeline.request_cancel(str(ws))  # cancel right after the first file

    s = pipeline.build(str(ws), progress=prog)
    assert s["cancelled"] is True
    assert 0 < s["files"] < 5  # stopped early, partial kept
    assert s.get("last_indexed_at") is None  # not marked complete
    # A normal rebuild afterward completes it (and clears the cancel flag).
    s2 = pipeline.build(str(ws))
    assert s2["cancelled"] is False and s2["files"] == 5 and s2.get("last_indexed_at")
