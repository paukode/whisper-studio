"""Tests for the words-first explorer views (server/index/explore_views.py):
the browse-pane entity rollup, the overview's named topic cards, entity pages
(facts as sentence-ready records with evidence), and file pages (per-file
neighbourhood computed without the whole-graph edge cap)."""

import numpy as np
import pytest

from server.index import paths, relstore, store
from server.index.config import EMBED_DIM


@pytest.fixture(autouse=True)
def _tmp_index_dir(tmp_path, monkeypatch):
    # Redirect index storage into the test's tmp dir so nothing touches data/.
    monkeypatch.setattr(paths, "INDEX_DATA_DIR", str(tmp_path / "index"))
    from server.infrastructure import model_mode

    _local = {"embed": "qwen3", "rerank": "qwen3", "ner": "gliner", "index_llm": "local"}
    monkeypatch.setattr(model_mode, "resolve_backend", lambda cap, config=None: _local[cap])


def _unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _add(ws: str, path: str, entities: list[dict], seed: int = 1, text: str = "text") -> None:
    store.replace_file(
        ws,
        path,
        {"hash": f"h-{path}-{seed}", "size": 1, "mtime": 1.0},
        [
            {
                "start_line": 1,
                "end_line": 3,
                "text": text,
                "vec": _unit(seed),
                "entities": entities,
            }
        ],
    )


def _set_salience(ws: str, name: str, value: float) -> None:
    conn = store._connect(ws)
    try:
        conn.execute("UPDATE nodes SET salience=? WHERE LOWER(name)=?", (value, name.lower()))
        conn.commit()
    finally:
        conn.close()
    store._invalidate(ws)


# ── entity list (browse pane) ────────────────────────────────────────────────


def test_entity_list_groups_by_label_and_counts_files():
    ws = "/fake/xpl-list"
    dana = {"name": "Dana Kim", "label": "person"}
    bank = {"name": "Northwind Bank", "label": "organization"}
    ledger = {"name": "Halloway Ledger", "label": "product"}
    rott = {"name": "Rotterdam", "label": "location"}
    _add(ws, "a.md", [dana, bank, ledger], seed=1)
    _add(ws, "b.md", [dana, bank], seed=2)
    _add(ws, "c.md", [dana, rott], seed=3)

    out = store.explore_entities(ws)
    groups = {g["key"]: g for g in out["groups"]}
    assert set(groups) == {"people", "organizations", "topics", "places"}
    people = groups["people"]["entities"]
    assert people[0]["name"] == "Dana Kim" and people[0]["files"] == 3
    assert [e["name"] for e in groups["organizations"]["entities"]] == ["Northwind Bank"]
    assert [e["name"] for e in groups["topics"]["entities"]] == ["Halloway Ledger"]
    assert [e["name"] for e in groups["places"]["entities"]] == ["Rotterdam"]
    assert out["hidden"] == 0 and out["total"] == 4


def test_entity_list_folds_case_variants_without_double_counting():
    """ "Dana Kim" and "dana kim" are distinct node rows; the rollup folds them
    into one entry whose file count is distinct files, not a sum."""
    ws = "/fake/xpl-fold"
    _add(ws, "a.md", [{"name": "Dana Kim", "label": "person"}], seed=1)
    _add(
        ws,
        "b.md",
        [{"name": "dana kim", "label": "person"}, {"name": "Dana Kim", "label": "person"}],
        seed=2,
    )

    out = store.explore_entities(ws)
    people = {e["name"].lower(): e for g in out["groups"] for e in g["entities"]}
    assert list(people) == ["dana kim"]
    assert people["dana kim"]["files"] == 2


def test_entity_list_hides_low_salience_but_counts_them():
    ws = "/fake/xpl-low"
    _add(
        ws,
        "a.md",
        [{"name": "Signal", "label": "product"}, {"name": "noise word", "label": "concept x"}],
    )
    _set_salience(ws, "noise word", 0.05)

    out = store.explore_entities(ws)
    names = [e["name"] for g in out["groups"] for e in g["entities"]]
    assert names == ["Signal"] and out["hidden"] == 1

    full = store.explore_entities(ws, include_low=True)
    names = [e["name"] for g in full["groups"] for e in g["entities"]]
    assert set(names) == {"Signal", "noise word"}


def test_entity_list_search_filters_by_substring():
    ws = "/fake/xpl-q"
    _add(
        ws,
        "a.md",
        [{"name": "Dana Kim", "label": "person"}, {"name": "Priya Shah", "label": "person"}],
    )
    out = store.explore_entities(ws, q="dana")
    names = [e["name"] for g in out["groups"] for e in g["entities"]]
    assert names == ["Dana Kim"]


def test_entity_list_drops_generic_category_words():
    ws = "/fake/xpl-generic"
    _add(
        ws, "a.md", [{"name": "person", "label": "person"}, {"name": "Dana Kim", "label": "person"}]
    )
    out = store.explore_entities(ws)
    names = [e["name"] for g in out["groups"] for e in g["entities"]]
    assert names == ["Dana Kim"]


# ── overview ─────────────────────────────────────────────────────────────────


def test_overview_names_topic_cards_and_ranks_files():
    """Two clusters of files sharing distinct entities become two named cards;
    stats and most-connected/stands-out lists come along."""
    ws = "/fake/xpl-overview"
    bank = {"name": "Northwind Bank", "label": "organization"}
    ledger = {"name": "Halloway Ledger", "label": "product"}
    parser = {"name": "FixedWidthReader", "label": "technology"}
    cobalt = {"name": "Cobalt mainframe", "label": "technology"}
    # cluster 1: three files about the bank + ledger
    _add(ws, "onboarding.md", [bank, ledger], seed=1)
    _add(ws, "runbook.md", [bank, ledger], seed=2)
    _add(ws, "faq.md", [bank], seed=3)
    # cluster 2: two files about the parser + mainframe
    _add(ws, "parser.md", [parser, cobalt], seed=4)
    _add(ws, "testing.md", [parser, cobalt], seed=5)

    out = store.explore_overview(ws)
    assert out["files"] == 5 and out["passages"] == 5 and out["entities"] == 4
    assert len(out["cards"]) == 2
    names = {tuple(sorted(c["names"])) for c in out["cards"]}
    flat = {n for card in names for n in card}
    assert "Northwind Bank" in flat and ("FixedWidthReader" in flat or "Cobalt mainframe" in flat)
    # a name can title only one card (greedy dedup across groups)
    all_names = [n for c in out["cards"] for n in c["names"]]
    assert len(all_names) == len(set(all_names))
    assert out["most_connected"][0]["links"] >= 1
    assert {e["name"] for e in out["stands_out"]} >= {"Northwind Bank"}
    for c in out["cards"]:
        assert c["files"] >= 2 and 1 <= len(c["top_files"]) <= 5


def test_overview_empty_index_has_no_cards():
    ws = "/fake/xpl-empty"
    _add(ws, "solo.md", [])
    out = store.explore_overview(ws)
    assert out["cards"] == [] and out["files"] == 1


# ── entity page ──────────────────────────────────────────────────────────────


def test_entity_page_canonical_name_facts_and_mentions():
    """A lowercased click resolves to the stored spelling; typed facts arrive
    sentence-ready (predicate, other, direction, files, cite with evidence);
    mention and related lists are ranked."""
    ws = "/fake/xpl-entity"
    dana = {"name": "Dana Kim", "label": "person"}
    bank = {"name": "Northwind Bank", "label": "organization"}
    _add(ws, "a.md", [dana, bank], seed=1, text="Dana Kim works at Northwind Bank")
    _add(ws, "b.md", [dana, bank], seed=2, text="Dana Kim of Northwind Bank")
    _add(ws, "c.md", [dana], seed=3)
    relstore.set_file_relations_v2(
        ws,
        "a.md",
        [
            {
                "source": "Dana Kim",
                "target": "Northwind Bank",
                "predicate": "works_at",
                "strength": 4,
                "evidence": "Dana Kim works at Northwind Bank",
                "start_line": 1,
                "end_line": 1,
            }
        ],
    )
    relstore.set_file_relations_v2(
        ws,
        "b.md",
        [
            {
                "source": "Dana Kim",
                "target": "Northwind Bank",
                "predicate": "works_at",
                "strength": 3,
                "evidence": "Dana Kim of Northwind Bank signed off",
                "start_line": 1,
                "end_line": 1,
            }
        ],
    )

    out = store.explore_entity(ws, "dana kim")
    assert out["found"] and out["name"] == "Dana Kim" and out["label"] == "person"
    assert len(out["facts"]) == 1
    f = out["facts"][0]
    assert f["predicate"] == "works_at" and f["other"] == "Northwind Bank"
    assert f["direction"] == "out" and f["files"] == 2
    assert f["cite"]["evidence"] and f["cite"]["name"] in {"a.md", "b.md"}
    assert [m["path"] for m in out["mentioned_in"]] == ["a.md", "b.md", "c.md"]
    assert all(m["passages"] == 1 for m in out["mentioned_in"])
    assert out["related"][0]["name"] == "Northwind Bank" and out["related"][0]["shared"] == 2


def test_entity_page_unknown_name_reports_not_found():
    ws = "/fake/xpl-entity-miss"
    _add(ws, "a.md", [{"name": "Dana Kim", "label": "person"}])
    assert store.explore_entity(ws, "nobody") == {"found": False}


# ── file page ────────────────────────────────────────────────────────────────


def test_file_page_lists_entities_neighbors_and_facts():
    ws = "/fake/xpl-file"
    dana = {"name": "Dana Kim", "label": "person"}
    bank = {"name": "Northwind Bank", "label": "organization"}
    ledger = {"name": "Halloway Ledger", "label": "product"}
    _add(ws, "a.md", [dana, bank, ledger], seed=1)
    _add(ws, "b.md", [bank, ledger], seed=2)
    _add(ws, "c.md", [dana], seed=3)
    relstore.set_file_relations_v2(
        ws,
        "a.md",
        [
            {
                "source": "Dana Kim",
                "target": "Northwind Bank",
                "predicate": "works_at",
                "strength": 4,
                "evidence": "Dana Kim works at Northwind Bank",
                "start_line": 2,
                "end_line": 2,
            }
        ],
    )

    out = store.explore_file(ws, "a.md")
    assert out["found"] and out["name"] == "a.md" and out["passages"] == 1
    assert {e["name"] for e in out["entities"]} == {"Dana Kim", "Northwind Bank", "Halloway Ledger"}
    nb = {n["path"]: n for n in out["neighbors"]}
    assert nb["b.md"]["shared"] == 2 and set(nb["b.md"]["entities"]) == {
        "Northwind Bank",
        "Halloway Ledger",
    }
    assert nb["c.md"]["shared"] == 1
    assert out["neighbors"][0]["path"] == "b.md"  # strongest first
    assert out["facts"] == [
        {
            "source": "Dana Kim",
            "target": "Northwind Bank",
            "predicate": "works_at",
            "strength": 4.0,
            "start_line": 2,
            "end_line": 2,
            "evidence": "Dana Kim works at Northwind Bank",
        }
    ]


def test_file_page_neighborhood_ignores_low_salience_entities():
    """The per-file neighbourhood applies the same good-entity filter as the
    overview graph: a below-floor entity creates no connection."""
    ws = "/fake/xpl-file-floor"
    noise = {"name": "boilerplate footer", "label": "concept x"}
    _add(ws, "a.md", [noise], seed=1)
    _add(ws, "b.md", [noise], seed=2)
    _set_salience(ws, "boilerplate footer", 0.05)
    out = store.explore_file(ws, "a.md")
    assert out["neighbors"] == []


def test_file_page_unknown_file_reports_not_found():
    ws = "/fake/xpl-file-miss"
    _add(ws, "a.md", [])
    assert store.explore_file(ws, "zzz.md") == {"found": False}


# ── HTTP routes ──────────────────────────────────────────────────────────────


def test_explore_routes_serve_views_with_root(monkeypatch):
    """The four /explore routes resolve the workspace, reject unindexed paths
    with empty shapes, and stamp the absolute root for the UI's reveal/open."""
    import asyncio

    from server.index import routes

    ws = "/fake/xpl-routes"
    _add(ws, "a.md", [{"name": "Dana Kim", "label": "person"}])
    monkeypatch.setattr(routes.paths, "is_indexed", lambda p: True)

    out = asyncio.run(routes.explore_overview(ws))
    assert out["files"] == 1 and out["root"] == ws
    out = asyncio.run(routes.explore_entities(ws))
    assert out["total"] == 1 and out["root"] == ws
    out = asyncio.run(routes.explore_entity(ws, "dana kim"))
    assert out["found"] and out["name"] == "Dana Kim" and out["root"] == ws
    out = asyncio.run(routes.explore_file(ws, "a.md"))
    assert out["found"] and out["passages"] == 1 and out["root"] == ws

    monkeypatch.setattr(routes.paths, "is_indexed", lambda p: False)
    assert asyncio.run(routes.explore_overview(ws))["files"] == 0
    assert asyncio.run(routes.explore_entity(ws, "dana kim")) == {"found": False, "root": ""}


# ── frontend verb map stays in sync with the predicate vocabulary ───────────


def test_fact_sentence_verb_map_covers_the_predicate_vocabulary():
    """The explorer renders facts as sentences via a TS verb map; every canonical
    predicate must have a phrase there, or new predicates silently fall back to
    underscore-mangled verbs in the UI."""
    import pathlib
    import re

    from server.index.relations_vocab import PREDICATES

    ts = (
        pathlib.Path(__file__).resolve().parent.parent
        / "src/components/workspace/explorer/factSentence.ts"
    ).read_text(encoding="utf-8")
    block = ts.split("export const VERB_PHRASES")[1].split("};")[0]
    ts_keys = set(re.findall(r"^\s*(\w+):", block, flags=re.M))
    missing = set(PREDICATES) - ts_keys
    assert not missing, f"factSentence.ts VERB_PHRASES is missing predicates: {sorted(missing)}"


# ── relstore addition ────────────────────────────────────────────────────────


def test_facts_for_entity_counts_distinct_files():
    """`files` counts distinct source files (the UI's confidence-in-words
    basis), independent of `sources` which counts observations."""
    ws = "/fake/xpl-factfiles"
    a = {"name": "Alice", "label": "person"}
    acme = {"name": "Acme", "label": "organization"}
    _add(ws, "one.md", [a, acme], seed=1, text="Alice works at Acme\nAlice still at Acme")
    fact = {"source": "Alice", "target": "Acme", "predicate": "works_at", "strength": 3}
    relstore.set_file_relations_v2(
        ws,
        "one.md",
        [
            {**fact, "evidence": "Alice works at Acme", "start_line": 1, "end_line": 1},
            {**fact, "evidence": "Alice still at Acme", "start_line": 2, "end_line": 2},
        ],
    )
    facts = relstore.facts_for_entity(ws, "alice")
    assert len(facts) == 1
    assert facts[0]["sources"] == 2 and facts[0]["files"] == 1
