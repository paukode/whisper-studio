"""Entity salience: the statistical noise defense for the GraphRAG graph.

Two jobs, both language-independent and free (no LLM):

  1. ``is_hard_junk(name, label)`` — an extract-time gate that rejects the
     unambiguous garbage GLiNER faithfully emits on a business-document corpus:
     code identifiers (snake_case, camelCase, dotted paths, file names), pure
     digits/punctuation, label-echoes ("person" tagged as a person), and a
     generic EN+PL stoplist. Everything ambiguous is kept and merely downweighted.

  2. ``recompute(ws_path)`` — a post-build pass that scores every node 0..1 from
     name shape × GLiNER confidence × inverse document frequency, and caps
     boilerplate hubs (an entity in a large fraction of all chunks). The score is
     persisted on ``nodes.salience`` and consumed by retrieval expansion, the
     graph views, and entity descriptions. Nodes are downweighted, never deleted
     (a refresh would only re-extract the same junk), so old index DBs upgrade in
     place on their next build.
"""

from __future__ import annotations

import logging
import math
import re

from .config import BOILERPLATE_DF_FRAC, SALIENCE_GRAPH_FLOOR, SALIENCE_JUNK_FLOOR

log = logging.getLogger("whisper-studio")

# ── extract-time hard-junk rules ─────────────────────────────────────────────

# Generic category words + pronouns that carry no relationship signal, in the two
# corpus languages (English + Polish). Superset of store._GENERIC_ENTITY_NAMES.
_HARD_STOPWORDS = frozenset(
    (
        # English generic / category words GLiNER tags as entities
        "person",
        "people",
        "organization",
        "organisation",
        "org",
        "company",
        "product",
        "products",
        "technology",
        "language",
        "library",
        "framework",
        "module",
        "method",
        "function",
        "class",
        "api",
        "apis",
        "service",
        "services",
        "database",
        "concept",
        "concepts",
        "file",
        "files",
        "protocol",
        "event",
        "events",
        "location",
        "object",
        "type",
        "types",
        "string",
        "variable",
        "parameter",
        "value",
        "values",
        "data",
        "system",
        "systems",
        "user",
        "users",
        "team",
        "teams",
        "customer",
        "customers",
        "project",
        "projects",
        "document",
        "documents",
        "report",
        "reports",
        "metric",
        "metrics",
        "role",
        "roles",
        "date",
        "name",
        "names",
        "email",
        "address",
        "information",
        "thing",
        "things",
        "item",
        "items",
        "work",
        "trust",
        "efficiency",
        "scalability",
        "reliability",
        "flexibility",
        "transparency",
        "clarity",
        "complexity",
        "context",
        "performance",
        # English pronouns / function words that leak in as entities
        "i",
        "we",
        "you",
        "he",
        "she",
        "it",
        "they",
        "them",
        "us",
        "me",
        # Polish generic / category words and pronouns
        "firma",
        "firmy",
        "spółka",
        "spółki",
        "spółce",
        "umowa",
        "umowy",
        "umowie",
        "klient",
        "klienci",
        "projekt",
        "projekty",
        "system",
        "dane",
        "kwota",
        "kwoty",
        "osoba",
        "osoby",
        "strona",
        "strony",
        "rola",
        "nazwa",
        "adres",
        "dokument",
        "dokumenty",
        "praca",
        "informacja",
        "informacje",
        "ja",
        "ty",
        "on",
        "ona",
        "ono",
        "my",
        "wy",
        "oni",
        "one",
    )
)

_DIGITS_ONLY = re.compile(r"[\d\s.,%-]+$")
_SNAKE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)+$")
# camelCase with >=2 humps ("getStockPrice"); one hump ("iPhone", "eBay") is spared.
_CAMEL = re.compile(r"[a-z]+[A-Z][a-z0-9]*(?:[A-Z][a-z0-9]*)+$")
_UPPER_SNAKE = re.compile(r"[A-Z0-9]+(?:_[A-Z0-9]+)+$")
_DOTTED = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
_HEX = re.compile(r"[0-9a-f]{8,}$")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_FILE_EXT = re.compile(
    r"\.(py|js|ts|tsx|jsx|mjs|cjs|json|ya?ml|toml|ini|cfg|xml|md|mdx|rst|txt|csv"
    r"|xlsx?|pdf|docx?|pptx?|html?|png|jpe?g|gif|svg|webp)$"
)


def _has_lower_segment(s: str) -> bool:
    """A dotted string with a genuinely lowercased segment is a code path / domain
    ("langchain.community", "company.com"), not an acronym ("U.S.", "S.A.")."""
    return any(len(seg) > 1 and seg[:1].islower() for seg in s.split("."))


def is_hard_junk(name: str, label: str = "") -> bool:
    """True for entities that are unambiguous noise and must never enter the graph.

    Conservative by design: only decidable-by-shape garbage is rejected here;
    everything else is kept and left to the graded salience score."""
    s = (name or "").strip()
    if len(s) < 2 or len(s) > 80:
        return True
    if _DIGITS_ONLY.fullmatch(s):
        return True
    if not any(c.isalnum() for c in s):  # pure punctuation
        return True
    low = s.lower()
    lab = (label or "").lower()
    if low in _HARD_STOPWORDS:
        return True
    if low == lab:  # label echo, e.g. name="person" label="person"
        return True
    if "/" in s or "\\" in s:  # path
        return True
    if _SNAKE.fullmatch(s) or _CAMEL.fullmatch(s) or _UPPER_SNAKE.fullmatch(s):
        return True
    if _HEX.fullmatch(low) or _UUID.fullmatch(low):
        return True
    if _DOTTED.fullmatch(s) and _has_lower_segment(s):
        return True
    if lab not in ("document", "file") and _FILE_EXT.search(low):
        return True
    if lab == "person":
        if any(c.isdigit() for c in s):
            return True
        if len(s.split()) > 4:
            return True
        if " " not in s and s == low:  # a single all-lowercase token is not a name
            return True
    return False


# ── graded salience ──────────────────────────────────────────────────────────


def shape_score(name: str, label: str = "") -> float:
    """Name-form prior in {0.0, 0.3, 0.6, 1.0}. Proper nouns and acronyms score
    high; single lowercase words and long vacuous phrases score low."""
    if is_hard_junk(name, label):
        return 0.0
    toks = name.strip().split()
    if any(t[:1].isupper() for t in toks):  # TitleCase, PROPER, or ACRONYM
        return 1.0
    if len(toks) == 1 or len(toks) >= 4:  # single lowercase word / long phrase
        return 0.3
    return 0.6  # 2–3 word lowercase phrase


def salience(shape: float, conf: float, idf_n: float) -> float:
    """Combine the three signals into a 0..1 score."""
    return shape * (0.4 + 0.6 * conf) * (0.35 + 0.65 * idf_n)


def recompute(ws_path: str) -> None:
    """Recompute and persist ``nodes.df`` and ``nodes.salience`` for a workspace.

    Pure function of the tables (chunk fanout + GLiNER span confidence + IDF), so
    it needs no backfill migration — a normal refresh backfills every node. Runs
    once per build, after entity dedup."""
    from . import store

    conn = store._connect(ws_path)
    try:
        n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] or 0
        rows = conn.execute("SELECT id, name, label FROM nodes").fetchall()
        if not rows:
            return
        # df (chunk fanout) and mean GLiNER confidence per node, in one scan.
        df: dict[int, int] = {}
        ssum: dict[int, float] = {}
        scnt: dict[int, int] = {}
        for nid, sc in conn.execute("SELECT node_id, score FROM node_chunks"):
            df[nid] = df.get(nid, 0) + 1
            if sc is not None:
                ssum[nid] = ssum.get(nid, 0.0) + sc
                scnt[nid] = scnt.get(nid, 0) + 1
        boil = max(store._MAX_NODE_FANOUT, BOILERPLATE_DF_FRAC * n_chunks)
        ln_n = math.log(n_chunks) if n_chunks > 1 else 0.0
        updates: list[tuple[int, float, int]] = []
        for nid, name, label in rows:
            d = df.get(nid, 0)
            conf = (ssum[nid] / scnt[nid]) if scnt.get(nid) else 0.7
            if n_chunks > 1 and d > 0:
                idf_n = max(0.0, min(1.0, math.log(n_chunks / d) / ln_n))
            else:
                idf_n = 1.0
            sal = salience(shape_score(name, label), conf, idf_n)
            if d > boil:  # boilerplate hub, regardless of name shape
                sal = min(sal, SALIENCE_JUNK_FLOOR)
            updates.append((d, round(sal, 4), nid))
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.executemany("UPDATE nodes SET df=?, salience=? WHERE id=?", updates)
        store._bump_write_gen(cur)
        conn.commit()
    finally:
        conn.close()
    store._invalidate(ws_path)
    log.info("Salience recomputed for %s (%d nodes)", ws_path, len(rows))


def entity_leg(ws_path: str, query_text: str, k: int = 8) -> list[dict]:
    """Retrieve chunks anchored to salient entities named in the query — a third
    retrieval leg beside dense + keyword. When the query mentions an entity (e.g.
    "Acme"), its chunks surface even if the wording differs from any passage.
    Query n-grams (1–3 words) are matched to salient node names by the same
    normalization the dedup uses; chunks are scored by summed entity salience so
    junk names contribute nothing. Purely lexical + statistical, no LLM."""
    from . import store

    if not (query_text or "").strip():
        return []

    # Normalize both query grams and node names the SAME way: dedup-normalize
    # (NFKC/case/possessive) then collapse any internal punctuation to spaces, so
    # names carrying '&', ',', or internal '.' (AT&T, "Acme, Inc.", S&P 500, Polish
    # "Sp. z o.o.") match the query — the query tokenizer drops that punctuation, so
    # the node side must too, or those entities silently never anchor.
    def _link_key(s: str) -> str:
        return " ".join(re.findall(r"\w+", store._norm_entity(s)))

    words = re.findall(r"\w[\w'-]*", query_text)
    grams = set()
    for n in (1, 2, 3):
        for i in range(len(words) - n + 1):
            g = _link_key(" ".join(words[i : i + n]))
            if len(g.replace(" ", "")) >= 3:
                grams.add(g)
    if not grams:
        return []
    conn = store._connect(ws_path)
    try:
        matched: dict[int, float] = {}
        for nid, name, sal in conn.execute(
            "SELECT id, name, COALESCE(salience, 0.5) FROM nodes WHERE COALESCE(salience, 0.5) >= ?",
            (SALIENCE_GRAPH_FLOOR,),
        ):
            if _link_key(name) in grams:
                matched[nid] = sal
        if not matched:
            return []
        node_ids = list(matched)
        marks = ",".join("?" * len(node_ids))
        rows = conn.execute(
            f"SELECT chunk_id, node_id FROM node_chunks WHERE node_id IN ({marks})", node_ids
        ).fetchall()
    finally:
        conn.close()
    scored: dict[int, float] = {}
    for cid, nid in rows:
        scored[cid] = scored.get(cid, 0.0) + matched.get(nid, 0.0)
    ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)[:k]
    chunks = {r["chunk_id"]: r for r in store._fetch_chunks(ws_path, [c for c, _ in ranked])}
    out = []
    for cid, w in ranked:
        r = chunks.get(cid)
        if r:
            r = dict(r)
            r["_ent"] = round(w, 4)
            out.append(r)
    return out
