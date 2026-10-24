"""Discover's Hugging Face search: official models first, then the trusted
publishers, ranked for the typed query.

The sources run side by side:
  * the publisher shards: each trusted GGUF and MLX publisher's matching repos
    (one explicit author, or all of Hugging Face, when asked);
  * a wide search per format, which also finds the model makers' own GGUF and
    MLX builds;
  * the official lane: the model makers' own repos (compat.OFFICIAL_AUTHORS)
    that match the query, and for the top ones the GGUF and MLX builds that
    declare them as their base model.

Every row passes the chat-pipeline and architecture gates, which read the
installed engines. An official model with no build the engines can run still
gets one row that says why, so a missing model is explained, not hidden. Rows
rank by how directly the name matches the query, then official before the
rest, then downloads (trending with no query).
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from server.model_browser import compat, hf_client

log = logging.getLogger("whisper-studio")

_SEARCH_LIMIT = 30
_PER_AUTHOR_LIMIT = 12
# Repos the wide search reads per format, and the official lane reads to find
# the model makers' own repos.
_WIDE_LIMIT = 60
_OFFICIAL_SCAN = 100
# Official models whose builds are looked up, and builds read per model and format.
_OFFICIAL_BASES = 5
_BUILDS_PER_BASE = 20

# Ranking groups inside one relevance tier, best first: an official model a
# trusted publisher built, the same by another publisher (all of Hugging Face
# only), an official model with no build the engines run, then the rest.
_OFFICIAL, _OFFICIAL_OTHER_PUBLISHER, _OFFICIAL_UNSUPPORTED, _TRUSTED, _OTHER = range(5)

_TRUSTED_PUBLISHERS = frozenset(
    a.lower() for a in (*compat.TRUSTED_AUTHORS, *compat.MLX_TRUSTED_AUTHORS)
)


def _squash(text: str) -> str:
    """Lowercase with every non-alphanumeric run removed, so "qwen 3.8",
    "qwen-3.8" and "Qwen3.8" all compare equal."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _relevance_tier(query: str, repo_id: str) -> int:
    """How directly a repo matches the typed query, spelling variants folded:
    3 = the repo name IS the query (or starts with it), 2 = the query appears
    inside the name, 1 = every query token appears somewhere in the full repo
    id, 0 = only a loose match (the Hub matched a field we don't score)."""
    q = _squash(query)
    if not q:
        return 0
    name_sq = _squash(_name(repo_id))
    if name_sq == q or name_sq.startswith(q):
        return 3
    if q in name_sq:
        return 2
    tokens = re.findall(r"[a-z0-9]+", query.lower())
    repo_sq = _squash(repo_id)
    if tokens and all(t in repo_sq for t in tokens):
        return 1
    return 0


def _author(repo_id: str) -> str:
    return repo_id.partition("/")[0]


def _name(repo_id: str) -> str:
    return repo_id.partition("/")[2] or repo_id


def _trusted(author: str) -> bool:
    return author.lower() in _TRUSTED_PUBLISHERS or compat.is_official_author(author)


def _runnable(fmt: str, hit: hf_client.SearchHit) -> bool:
    arch_ok = compat.mlx_arch_supported if fmt == "mlx" else compat.arch_supported
    return compat.chat_pipeline(hit.pipeline_tag) and arch_ok(hit.arch)


def _install_annotations(fmt: str) -> dict[str, dict]:
    """``repo_id -> {"installed": [filenames], "downloading": [filenames]}`` for
    every registry model of this format, so search rows can say "you already
    have this" instead of letting the user queue the same multi-GB download
    twice. Download state comes from the models-manager queue (the SAME source
    the Chat tab renders). Filenames let the GGUF quant picker mark the exact
    quant; MLX entries have none (the repo is the unit). Never raises: an
    annotation failure must not sink the search."""
    try:
        from server.local.registry import local_models
        from server.models_manager import catalog, manager

        by_key = {e.key: e for e in catalog.entries() if e.group == catalog.GROUP_LOCAL_CHAT}
        out: dict[str, dict] = {}
        for key, m in local_models().items():
            if (m.get("engine") == "mlx") != (fmt == "mlx"):
                continue
            entry = by_key.get(key)
            if entry is None:
                continue
            state, _err = manager.state_of(entry)
            slot = out.setdefault(m["repo_id"], {"installed": [], "downloading": []})
            if state == "installed":
                slot["installed"].append(m.get("filename") or "")
            elif state in ("downloading", "queued"):
                slot["downloading"].append(m.get("filename") or "")
        return out
    except Exception as e:  # pragma: no cover - defensive
        log.debug("Could not annotate install states: %s", e)
        return {}


def _official_bases(query: str | None) -> list[hf_client.SearchHit]:
    """The model makers' own chat models that match the query, best first. A
    maker's own GGUF or MLX repo is a build, not a base: the wide search
    finds it as a row of its own."""
    bases = [
        h
        for h in hf_client.official_models(query, _OFFICIAL_SCAN)
        if compat.is_official_author(_author(h.repo_id))
        and compat.chat_pipeline(h.pipeline_tag)
        and not h.is_build
    ]
    if query:
        bases = [h for h in bases if _relevance_tier(query, h.repo_id) > 0]
        bases.sort(key=lambda h: (-_relevance_tier(query, h.repo_id), -(h.downloads or 0)))
    return bases[:_OFFICIAL_BASES]


def _unsupported_reason(builds: list[tuple[str, hf_client.SearchHit]], all_of_hf: bool) -> str:
    """Why an official model has no row the user can install."""
    if not builds:
        return "There is no GGUF or MLX build of this model on Hugging Face yet."
    if any(_runnable(fmt, h) for fmt, h in builds) and not all_of_hf:
        return "Only community builds exist. Turn on All of Hugging Face to see them."
    arch = next((h.arch for _fmt, h in builds if h.arch), None)
    what = f"the {arch} architecture" if arch else "this model's architecture"
    return f"The installed llama.cpp and mlx-lm cannot run {what} yet."


def _row(fmt: str | None, hit: hf_client.SearchHit, base_model: str | None) -> dict:
    author = _author(hit.repo_id)
    name = _name(hit.repo_id)
    official = compat.is_official_author(author) or bool(
        base_model and compat.is_official_author(_author(base_model))
    )
    return {
        "repo_id": hit.repo_id,
        "author": author,
        "name": name,
        "label": name,
        "format": fmt,
        "downloads": hit.downloads,
        "likes": hit.likes,
        "trending_score": hit.trending_score,
        "arch": hit.arch,
        "param_size": compat.param_size_label(hit.repo_id),
        # Whether this model would be installed with tools ON, so the row can
        # say "chat only" BEFORE a multi-GB download rather than leaving the
        # user to discover it from a broken first turn.
        "tool_capable": compat.is_agentic_capable(hit.repo_id),
        "context_length": hit.context_length,
        "gated": hit.gated,
        "supported": True,
        "official": official,
        "base_model": base_model,
        "unsupported_reason": None,
    }


def _group(row: dict) -> int:
    if not row["supported"]:
        return _OFFICIAL_UNSUPPORTED
    trusted = _trusted(row["author"])
    if row["official"]:
        return _OFFICIAL if trusted else _OFFICIAL_OTHER_PUBLISHER
    return _TRUSTED if trusted else _OTHER


def _results(future) -> list:
    try:
        return future.result()
    except Exception as e:  # pragma: no cover - the client never raises
        log.debug("Search source failed: %s", e)
        return []


def search(
    query: str | None = None,
    author: str | None = None,
    all_of_hf: bool = False,
) -> list[dict]:
    """Search chat repos across BOTH weight formats at once.

    Scope:
      * explicit ``author``: just that author, both formats, no official lane;
      * default (``all_of_hf`` false): the trusted publishers and the model
        makers, each format and the official lane;
      * ``all_of_hf`` true: every publisher, each format and the official lane.
    """
    query = (query or "").strip() or None
    if author:
        shards = [("gguf", author, _SEARCH_LIMIT), ("mlx", author, _SEARCH_LIMIT)]
    else:
        shards = [("gguf", None, _WIDE_LIMIT), ("mlx", None, _WIDE_LIMIT)]
        if not all_of_hf:
            shards += [("gguf", a, _PER_AUTHOR_LIMIT) for a in compat.TRUSTED_AUTHORS]
            shards += [("mlx", a, _PER_AUTHOR_LIMIT) for a in compat.MLX_TRUSTED_AUTHORS]

    with ThreadPoolExecutor(max_workers=len(shards) + 1 + 2 * _OFFICIAL_BASES) as pool:
        shard_futures = [
            (f, pool.submit(hf_client.search, query, a, limit, f)) for f, a, limit in shards
        ]
        bases = [] if author else _results(pool.submit(_official_bases, query))
        build_futures = [
            (f, base, pool.submit(hf_client.conversions, base.repo_id, f, _BUILDS_PER_BASE))
            for base in bases
            for f in ("gguf", "mlx")
        ]
        hits = [(f, h, h.base_model) for f, future in shard_futures for h in _results(future)]
        builds_of: dict[str, list[tuple[str, hf_client.SearchHit]]] = {b.repo_id: [] for b in bases}
        for f, base, future in build_futures:
            for h in _results(future):
                builds_of[base.repo_id].append((f, h))
                hits.append((f, h, base.repo_id))

    # GGUF hits ahead of MLX so a repo publishing both formats de-dupes into
    # the lane with the per-quant picker (stable sort keeps source order).
    hits.sort(key=lambda item: item[0] != "gguf")

    seen: set[str] = set()
    out: list[dict] = []
    for fmt, h, base_model in hits:
        if h.repo_id in seen:
            continue
        seen.add(h.repo_id)
        # The gates: the whole point of the browser is to not offer a model
        # that fails at load. Outside "all of Hugging Face", only the trusted
        # publishers and the model makers.
        if not _runnable(fmt, h):
            continue
        if not all_of_hf and not author and not _trusted(_author(h.repo_id)):
            continue
        out.append(_row(fmt, h, base_model))

    covered = {r["base_model"] for r in out} | {r["repo_id"] for r in out}
    for base in bases:
        if base.repo_id in covered:
            continue
        builds = builds_of[base.repo_id]
        row = _row(None, base, None)
        row.update(
            supported=False,
            arch=next((h.arch for _f, h in builds if h.arch), None),
            tool_capable=None,
            unsupported_reason=_unsupported_reason(builds, all_of_hf),
        )
        out.append(row)

    annotations = {f: _install_annotations(f) for f in ("gguf", "mlx")}
    for row in out:
        ann = annotations[row["format"]].get(row["repo_id"], {}) if row["format"] else {}
        installed = ann.get("installed") or []
        downloading = ann.get("downloading") or []
        row["installed_filenames"] = installed
        row["downloading_filenames"] = downloading
        row["install_state"] = "installed" if installed else "downloading" if downloading else None

    def tier(row: dict) -> int:
        own = _relevance_tier(query, row["repo_id"])
        base = _relevance_tier(query, row["base_model"]) if row["base_model"] else 0
        return max(own, base)

    if query:
        out.sort(key=lambda r: (-tier(r), _group(r), -(r.get("downloads") or 0)))
    else:
        out.sort(key=lambda r: (_group(r), -(r.get("trending_score") or 0)))
    return out[:_SEARCH_LIMIT]
