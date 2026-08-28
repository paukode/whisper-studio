"""Thin Hugging Face Hub wrappers for the model browser.

Every network call the browser makes lives here so the service layer stays pure
orchestration and the tests can monkeypatch a single seam (no HTTP in tests).
This is an extension of the existing ``huggingface_hub`` usage in
server/models_manager/sizes.py, not a new dependency.

Two facts about ``huggingface_hub`` 1.x shape this module:
  * ``list_models`` no longer takes ``library``/``direction``; GGUF repos are
    selected with ``filter="gguf"`` and the sort direction is implied by
    ``sort`` (descending).
  * ``model_info(expand=[...])`` and ``model_info(files_metadata=True)`` are
    mutually exclusive, so architecture/context (from ``expand=["gguf"]``) and
    per-file sizes (from ``files_metadata=True``) come from two separate calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("whisper-studio")

# What the search asks the Hub to hydrate on each hit — enough for the result
# card and the arch gate without a follow-up call. The MLX variant swaps the
# GGUF header for the transformers config (model_type lives there).
_SEARCH_EXPAND = ["downloads", "likes", "trendingScore", "gguf", "lastModified"]
_SEARCH_EXPAND_MLX = ["downloads", "likes", "trendingScore", "config", "lastModified"]


@dataclass
class SearchHit:
    repo_id: str
    downloads: int | None
    likes: int | None
    trending_score: int | None
    arch: str | None
    context_length: int | None
    gated: bool


@dataclass
class RepoGguf:
    """GGUF header metadata for a repo (representative file), from expand=gguf."""

    arch: str | None
    context_length: int | None
    has_chat_template: bool


def _gguf_field(gguf, key: str):
    """Read a field from the ``gguf`` metadata, which HF returns as a dict (and,
    defensively, could be an attribute-carrying object)."""
    if gguf is None:
        return None
    if isinstance(gguf, dict):
        return gguf.get(key)
    return getattr(gguf, key, None)


def _api():
    from huggingface_hub import HfApi

    return HfApi()


def search(
    query: str | None,
    author: str | None,
    sort: str,
    limit: int,
    fmt: str = "gguf",
) -> list[SearchHit]:
    """One ``list_models`` call, text-generation repos of one weight format.

    ``fmt`` picks the lane: "gguf" filters on the GGUF tag and reads arch/ctx
    from the GGUF header; "mlx" filters on the MLX library tag and reads the
    arch (``model_type``) from the transformers config. ``sort`` is
    "trendingScore" or "downloads"; the Hub sorts descending. Any failure
    (offline, bad author) returns an empty list — the caller merges across
    authors and a single failing author must not sink the whole search.
    """
    mlx = fmt == "mlx"
    try:
        models = _api().list_models(
            filter="mlx" if mlx else "gguf",
            pipeline_tag="text-generation",
            search=query or None,
            author=author or None,
            sort=sort,
            limit=limit,
            expand=_SEARCH_EXPAND_MLX if mlx else _SEARCH_EXPAND,
        )
    except Exception as e:
        log.debug("HF model search failed (author=%s, q=%r): %s", author, query, e)
        return []

    hits: list[SearchHit] = []
    for m in models:
        if mlx:
            config = getattr(m, "config", None) or {}
            arch = config.get("model_type") if isinstance(config, dict) else None
            context_length = None
        else:
            gguf = getattr(m, "gguf", None)
            arch = _gguf_field(gguf, "architecture")
            context_length = _gguf_field(gguf, "context_length")
        hits.append(
            SearchHit(
                repo_id=m.id,
                downloads=getattr(m, "downloads", None),
                likes=getattr(m, "likes", None),
                trending_score=getattr(m, "trending_score", None),
                arch=arch,
                context_length=context_length,
                gated=bool(getattr(m, "gated", False)),
            )
        )
    return hits


def repo_gguf_meta(repo_id: str) -> RepoGguf:
    """Architecture + context length + chat-template presence for a repo, read
    from the GGUF header via ``expand=["gguf"]`` (no file download)."""
    try:
        info = _api().model_info(repo_id, expand=["gguf"])
    except Exception as e:
        log.debug("HF gguf meta fetch failed for %s: %s", repo_id, e)
        return RepoGguf(arch=None, context_length=None, has_chat_template=False)
    gguf = getattr(info, "gguf", None)
    return RepoGguf(
        arch=_gguf_field(gguf, "architecture"),
        context_length=_gguf_field(gguf, "context_length"),
        has_chat_template=bool(_gguf_field(gguf, "chat_template")),
    )


def repo_model_type(repo_id: str) -> str | None:
    """The transformers-config ``model_type`` for a repo (the MLX arch gate),
    from ``expand=["config"]``. None on any failure."""
    try:
        info = _api().model_info(repo_id, expand=["config"])
    except Exception as e:
        log.debug("HF config fetch failed for %s: %s", repo_id, e)
        return None
    config = getattr(info, "config", None) or {}
    return config.get("model_type") if isinstance(config, dict) else None


def repo_files(repo_id: str) -> list[tuple[str, int]]:
    """``[(filename, size_bytes)]`` for every file in the repo (sizes from
    ``files_metadata=True``). Empty list on any failure."""
    try:
        info = _api().model_info(repo_id, files_metadata=True)
    except Exception as e:
        log.debug("HF file listing failed for %s: %s", repo_id, e)
        return []
    out: list[tuple[str, int]] = []
    for s in getattr(info, "siblings", None) or []:
        out.append((s.rfilename, int(getattr(s, "size", 0) or 0)))
    return out


def repo_is_gated(repo_id: str) -> bool:
    """Whether the repo is gated (needs license acceptance + a token). Best
    effort — an unknown answer is treated as not gated."""
    try:
        info = _api().model_info(repo_id)
    except Exception:
        return False
    return bool(getattr(info, "gated", False))
