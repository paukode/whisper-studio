"""Ask the manual: retrieval over the bundled documentation site.

The static site in ``docs/`` ships inside the app (mounted at ``/docs-site``;
see server/main.py). An explicit ``@docs`` mention in chat retrieves the
manual sections relevant to the question and injects them as a STRICT
grounding block: the model answers only from the manual, cites pages with
``#docspage=`` links (the chat renderer opens them in the right-pane docs
viewer), and says so when the manual has no answer instead of guessing.

The section index is small (heading-level sections across ~55 pages),
embedded with the active embed backend and cached under
``data_root()/docs-index`` as one file pair per backend, keyed by a
files signature so changed docs (a new app version) rebuild automatically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import urllib.parse

import numpy as np

log = logging.getLogger("whisper-studio")

# Repo root in dev; Resources/backend in the packaged app — both put docs/
# next to server/ (build_app.sh stages it), so one relative hop works in both.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Sections under the cosine floor are noise, not answers. Deliberately higher
# than the workspace-index floor: the manual is small and on-topic questions
# score well, while a floor this high makes "no relevant passages" (and with
# it the honest "the docs don't cover this") the common miss outcome.
# Calibrated on qwen3-embedding-0.6b over the real manual: on-topic questions
# score 0.56 to 0.72, unrelated ones 0.34 and below; 0.45 splits both with margin.
_SCORE_FLOOR = 0.45
_TOP_K = 6
_SECTION_MAX_CHARS = 1400

# Pages that aren't manual content.
_SKIP_PAGES = {"contributing.html"}

_lock = threading.Lock()
_cache: dict | None = None  # {"signature", "sections", "vectors"}


def docs_dir() -> str | None:
    """The bundled docs site, or None when absent (source checkout without
    docs, or a build that excluded them). ``WHISPER_DOCS_DIR`` overrides."""
    env = os.environ.get("WHISPER_DOCS_DIR", "").strip()
    d = os.path.abspath(os.path.expanduser(env)) if env else os.path.join(_ROOT, "docs")
    return d if os.path.isfile(os.path.join(d, "index.html")) else None


def _signature(d: str) -> str:
    """Cheap change signature over the doc pages (name, size, mtime)."""
    h = hashlib.sha1()
    for name in sorted(os.listdir(d)):
        if not name.endswith(".html"):
            continue
        st = os.stat(os.path.join(d, name))
        h.update(f"{name}|{st.st_size}|{int(st.st_mtime)}".encode())
    return h.hexdigest()


def _page_sections(path: str, page: str) -> list[dict]:
    """Split one page into heading-level sections.

    The docs pages are uniform: one ``<main>`` with an ``<h1>`` page title and
    ``<h2 id=...>`` sections (h3 subsections fold into their h2). The intro
    before the first h2 becomes the page's lead section with no anchor.
    """
    from bs4 import BeautifulSoup

    with open(path, encoding="utf-8", errors="replace") as f:
        soup = BeautifulSoup(f.read(), "html.parser")
    main = soup.find("main") or soup.body or soup
    for tag in main.find_all(["script", "style", "nav"]):
        tag.decompose()
    h1 = main.find("h1")
    page_title = h1.get_text(" ", strip=True) if h1 else page

    sections: list[dict] = []
    cur = {"title": page_title, "anchor": "", "parts": []}

    def _flush():
        text = re.sub(r"\s+", " ", " ".join(cur["parts"])).strip()
        if text:
            sections.append(
                {
                    "page": page,
                    "page_title": page_title,
                    "title": cur["title"],
                    "anchor": cur["anchor"],
                    "text": text[:_SECTION_MAX_CHARS],
                }
            )

    for el in main.find_all(["h2", "p", "li", "pre", "td", "dt", "dd", "h3"]):
        if el.name == "h2":
            _flush()
            cur = {
                "title": el.get_text(" ", strip=True),
                "anchor": el.get("id") or "",
                "parts": [],
            }
        else:
            cur["parts"].append(el.get_text(" ", strip=True))
    _flush()
    return sections


def _extract_sections(d: str) -> list[dict]:
    out: list[dict] = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".html") or name in _SKIP_PAGES:
            continue
        try:
            out.extend(_page_sections(os.path.join(d, name), name))
        except Exception as e:  # noqa: BLE001 — one broken page must not kill the manual
            log.warning("docs_qa: could not parse %s: %s", name, e)
    return out


def _index_paths() -> tuple[str, str]:
    from server.index.embedder import _embed_backend
    from server.infrastructure.paths import data_root

    base = os.path.join(data_root(), "docs-index")
    os.makedirs(base, exist_ok=True)
    backend = _embed_backend()
    return os.path.join(base, f"qa-{backend}.json"), os.path.join(base, f"qa-{backend}.npz")


def ensure_index() -> tuple[list[dict], np.ndarray] | None:
    """Load the cached section index, (re)building it when the docs changed.
    Returns None when no docs are bundled. Blocking on first build (embedder
    cold load), so call it off the event loop."""
    global _cache
    d = docs_dir()
    if d is None:
        return None
    sig = _signature(d)
    with _lock:
        if _cache is not None and _cache["signature"] == sig:
            return _cache["sections"], _cache["vectors"]
        meta_path, vec_path = _index_paths()
        if os.path.exists(meta_path) and os.path.exists(vec_path):
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
                if meta.get("signature") == sig:
                    vectors = np.load(vec_path)["vectors"]
                    if len(meta["sections"]) == len(vectors):
                        _cache = {
                            "signature": sig,
                            "sections": meta["sections"],
                            "vectors": vectors,
                        }
                        return _cache["sections"], _cache["vectors"]
            except Exception as e:  # noqa: BLE001 — a corrupt cache just rebuilds
                log.warning("docs_qa: cache unreadable, rebuilding: %s", e)

        from server.index import embedder

        sections = _extract_sections(d)
        if not sections:
            return None
        log.info("docs_qa: embedding %d manual sections (first use or docs changed)", len(sections))
        texts = [f"{s['page_title']} > {s['title']}\n{s['text']}" for s in sections]
        vectors = embedder.embed_documents(texts)
        # Match the workspace index build: never keep the embedder resident
        # just because the manual was indexed once.
        embedder.unload()
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"signature": sig, "sections": sections}, f)
        np.savez_compressed(vec_path, vectors=vectors)
        _cache = {"signature": sig, "sections": sections, "vectors": vectors}
        return sections, vectors


def _docspage_link(s: dict) -> str:
    frag = urllib.parse.quote(s["page"], safe="")
    if s["anchor"]:
        frag += "&h=" + urllib.parse.quote(s["anchor"], safe="")
    title = s["title"] if s["title"] == s["page_title"] else f"{s['page_title']} · {s['title']}"
    return f"[{title}](#docspage={frag})"


def retrieve(question: str, k: int = _TOP_K) -> list[dict] | None:
    """Top manual sections for the question (cosine over the section index,
    floor-gated), or None when no docs are bundled."""
    loaded = ensure_index()
    if loaded is None:
        return None
    sections, vectors = loaded
    from server.index import embedder

    q = embedder.embed_query(question)
    qn = float(np.linalg.norm(q)) or 1.0
    norms = np.linalg.norm(vectors, axis=1)
    norms[norms == 0] = 1.0
    scores = (vectors @ q) / (norms * qn)
    order = np.argsort(scores)[::-1][:k]
    out = []
    for i in order:
        if float(scores[i]) < _SCORE_FLOOR:
            break
        s = dict(sections[int(i)])
        s["score"] = round(float(scores[i]), 4)
        out.append(s)
    return out


def grounding_block(question: str) -> tuple[str, int]:
    """The prompt block for a ``@docs`` turn: retrieved manual passages under a
    strict answer-only-from-the-manual contract, or the explicit no-answer
    instruction when the manual has nothing relevant (so the model declines
    instead of improvising). Returns ``(block, passage_count)``; the block is
    '' when no docs are bundled with this build."""
    hits = retrieve(question)
    if hits is None:
        return "", 0
    if not hits:
        return (
            "[App documentation lookup (@docs). No relevant passages exist in "
            "the app's documentation for this question. Tell the user, in one "
            "short sentence, that the documentation does not cover this, and "
            "do not answer the question from general knowledge or guesses.]",
            0,
        )
    out = [
        "[App documentation context (@docs). The passages below, from the "
        "app's own manual, are the ONLY permitted source for this answer: "
        "answer strictly from them, never from general knowledge or guesses "
        "about the app. If they do not actually answer the question, say in "
        "one short sentence that the documentation does not cover it, and "
        "stop. When you do answer, end with a 'Sources' section listing each "
        "page you drew on as a markdown link copied EXACTLY as given below — "
        "the #docspage= href opens that page inside the app.]",
        "",
    ]
    for i, s in enumerate(hits, 1):
        out.append(f"{i}. {_docspage_link(s)}\n   {s['text']}")
    return "\n".join(out), len(hits)
