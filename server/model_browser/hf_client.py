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
_SEARCH_EXPAND = ["downloads", "likes", "trendingScore", "gguf", "pipeline_tag", "baseModels"]
_SEARCH_EXPAND_MLX = ["downloads", "likes", "trendingScore", "config", "pipeline_tag", "baseModels"]
# The official lane reads the model makers' own repos, which are usually
# original weights in neither format: tags and library only tell a GGUF or MLX
# build apart from a base model.
_OFFICIAL_EXPAND = [
    "downloads",
    "likes",
    "trendingScore",
    "pipeline_tag",
    "gated",
    "tags",
    "library_name",
]

# Bytes fetched per ranged request when reading a GGUF header. The model keys
# end within the first few KB, before the tokenizer vocabulary.
_HEADER_BLOCK = 64 << 10


@dataclass
class SearchHit:
    repo_id: str
    downloads: int | None
    likes: int | None
    trending_score: int | None
    arch: str | None
    context_length: int | None
    gated: bool
    pipeline_tag: str | None = None
    # The repo this one is a quantized build of (Hugging Face's base_model
    # relation), when the repo declares one.
    base_model: str | None = None
    # True when the repo holds GGUF or MLX weights (set by official_models).
    is_build: bool = False


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


def _quantized_base(m) -> str | None:
    """The base model id when the repo declares itself a quantized build."""
    base = getattr(m, "base_models", None)
    if isinstance(base, dict) and base.get("relation") == "quantized":
        models = base.get("models") or []
        if models and isinstance(models[0], dict):
            return models[0].get("id")
    return None


def _hits(models, mlx: bool) -> list[SearchHit]:
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
                pipeline_tag=getattr(m, "pipeline_tag", None),
                base_model=_quantized_base(m),
            )
        )
    return hits


def search(
    query: str | None,
    author: str | None,
    limit: int,
    fmt: str = "gguf",
) -> list[SearchHit]:
    """One ``list_models`` call for repos of one weight format.

    ``fmt`` picks the lane: "gguf" filters on the GGUF tag and reads arch/ctx
    from the GGUF header; "mlx" filters on the MLX library tag and reads the
    arch (``model_type``) from the transformers config. No pipeline filter: new
    chat repos are often tagged "image-text-to-text" or not at all, so the
    caller decides from the tag and the architecture. The Hub sorts by
    trending only so the ``limit`` window keeps the most notable matches; the
    final order is the caller's. Any failure (offline, bad author) returns an
    empty list, so one failing shard cannot sink the whole search.
    """
    mlx = fmt == "mlx"
    try:
        models = _api().list_models(
            filter="mlx" if mlx else "gguf",
            search=query or None,
            author=author or None,
            sort="trendingScore",
            limit=limit,
            expand=_SEARCH_EXPAND_MLX if mlx else _SEARCH_EXPAND,
        )
        return _hits(models, mlx)
    except Exception as e:
        log.debug("HF model search failed (author=%s, q=%r): %s", author, query, e)
        return []


def official_models(query: str | None, limit: int) -> list[SearchHit]:
    """Repos of any format that match ``query``, most downloaded first (most
    trending with no query), for the caller to keep the official ones. One
    unscoped call: the Hub takes one author per call, and the model makers'
    own repos lead the downloads for their model names."""
    try:
        models = _api().list_models(
            search=query or None,
            sort="downloads" if query else "trendingScore",
            limit=limit,
            expand=_OFFICIAL_EXPAND,
        )
        return [
            SearchHit(
                repo_id=m.id,
                downloads=getattr(m, "downloads", None),
                likes=getattr(m, "likes", None),
                trending_score=getattr(m, "trending_score", None),
                arch=None,
                context_length=None,
                gated=bool(getattr(m, "gated", False)),
                pipeline_tag=getattr(m, "pipeline_tag", None),
                is_build="gguf" in (getattr(m, "tags", None) or [])
                or getattr(m, "library_name", None) == "mlx",
            )
            for m in models
        ]
    except Exception as e:
        log.debug("HF official model search failed (q=%r): %s", query, e)
        return []


def conversions(base_model: str, fmt: str, limit: int) -> list[SearchHit]:
    """The GGUF or MLX repos that declare themselves quantized builds of
    ``base_model``, most downloaded first."""
    mlx = fmt == "mlx"
    try:
        models = _api().list_models(
            filter=["mlx" if mlx else "gguf", f"base_model:quantized:{base_model}"],
            sort="downloads",
            limit=limit,
            expand=_SEARCH_EXPAND_MLX if mlx else _SEARCH_EXPAND,
        )
        return _hits(models, mlx)
    except Exception as e:
        log.debug("HF conversion lookup failed (%s, %s): %s", base_model, fmt, e)
        return []


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


def gguf_file_metadata(repo_id: str, filename: str) -> dict | None:
    """The header key/values of one GGUF file in a repo, read with ranged
    requests (no download). ``expand=["gguf"]`` returns only the architecture,
    the context length and the chat template, and the KV-cache size needs the
    layer, head and window keys. The reader stops at the tokenizer vocabulary,
    so this is one request of ``_HEADER_BLOCK`` bytes for a usual header.
    None on any failure."""
    from huggingface_hub import HfFileSystem

    from server.model_browser.gguf_kv import read_metadata

    try:
        with HfFileSystem().open(f"{repo_id}/{filename}", "rb", block_size=_HEADER_BLOCK) as f:
            return read_metadata(f)
    except Exception as e:
        log.debug("GGUF header read failed for %s/%s: %s", repo_id, filename, e)
        return None


def repo_mlx_meta(repo_id: str) -> tuple[str | None, bool]:
    """``(model_type, is_mlx_tagged)`` for a repo, from one model_info call.

    ``model_type`` (transformers config) is the MLX arch gate; the ``mlx``
    library tag proves the repo actually carries MLX-format weights — a GGUF
    repo often has a config.json too, and installing it through the MLX lane
    would snapshot gigabytes mlx_lm cannot load. ``(None, False)`` on failure.
    """
    try:
        info = _api().model_info(repo_id, expand=["config", "tags"])
    except Exception as e:
        log.debug("HF config fetch failed for %s: %s", repo_id, e)
        return None, False
    config = getattr(info, "config", None) or {}
    model_type = config.get("model_type") if isinstance(config, dict) else None
    tags = getattr(info, "tags", None) or []
    return model_type, "mlx" in tags


def repo_config_context_length(repo_id: str) -> int | None:
    """``max_position_embeddings`` from the repo's config.json — the real
    context window an MLX model was configured with. The file is a few KB, so
    fetching it at install time is cheap. None on any failure."""
    try:
        import json

        from huggingface_hub import hf_hub_download

        path = hf_hub_download(repo_id=repo_id, filename="config.json")
        with open(path) as f:
            cfg = json.load(f)
        value = cfg.get("max_position_embeddings")
        if not value and isinstance(cfg.get("text_config"), dict):
            value = cfg["text_config"].get("max_position_embeddings")
        return int(value) if value else None
    except Exception as e:
        log.debug("HF config.json fetch failed for %s: %s", repo_id, e)
        return None


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
