"""Model mode + per-capability backend resolution.

A single ``model_mode`` decides where each indexing/RAG capability runs:

  - ``"cloud"``  — all Amazon Bedrock: embed=cohere, rerank=cohere, ner=haiku,
                   index_llm=haiku
  - ``"local"``  — all on-device: embed=qwen3, rerank=qwen3, ner=gliner,
                   index_llm=local
  - ``"hybrid"`` — per-capability, read from the config ``backends`` map; any
                   capability left unset falls back to the cloud backend.

Chat-model routing is independent of this (a model's own ``provider`` marker +
the mode-aware visibility filter below decide that), so this module covers only
the four index/RAG capabilities.

Phase note: the per-capability resolution is the single source of truth the
embedder/reranker/NER factories will read. Until the Bedrock backends are wired
in, callers may still run the on-device implementation regardless of mode; the
per-index backend stamp records what ACTUALLY ran, so a later mode flip queues a
rebuild only where the stamp differs.
"""

from __future__ import annotations

MODES = ("cloud", "hybrid", "local")
CAPABILITIES = ("embed", "rerank", "ner", "index_llm")

# Canonical backend per capability for the two pure modes.
_CLOUD: dict[str, str] = {
    "embed": "cohere",
    "rerank": "cohere",
    "ner": "haiku",
    "index_llm": "haiku",
}
_LOCAL: dict[str, str] = {
    "embed": "qwen3",
    "rerank": "qwen3",
    "ner": "gliner",
    "index_llm": "local",
}

DEFAULT_MODE = "cloud"


def _cfg(config: dict | None) -> dict:
    if config is not None:
        return config
    from server.infrastructure.config import load_config

    return load_config()


def current_mode(config: dict | None = None) -> str:
    """The active model mode, coerced to a known value (default ``cloud``)."""
    mode = _cfg(config).get("model_mode") or DEFAULT_MODE
    return mode if mode in MODES else DEFAULT_MODE


def resolve_backend(capability: str, config: dict | None = None) -> str:
    """Backend name for ``capability`` under the active mode.

    cloud/local map every capability to their canonical backend; hybrid reads
    the per-capability ``backends`` override and falls back to the cloud backend
    when a capability is unset.
    """
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown capability: {capability!r}")
    cfg = _cfg(config)
    mode = current_mode(cfg)
    if mode == "local":
        return _LOCAL[capability]
    if mode == "cloud":
        return _CLOUD[capability]
    # hybrid — per-capability picks, default to the cloud backend when unset.
    overrides = cfg.get("backends") or {}
    chosen = overrides.get(capability)
    return chosen if isinstance(chosen, str) and chosen else _CLOUD[capability]


def visible_chat_keys(model_keys, meta: dict, mode: str) -> list:
    """Filter chat-model keys for the model picker by mode.

    cloud hides on-device models (no local runtime to serve them); local hides
    cloud models (everything runs on-device); hybrid shows all. If the filter
    would empty the list (e.g. a cloud install left in local mode with no
    on-device models), fall back to showing all so the picker is never blank.
    """
    keys = list(model_keys)
    if mode == "hybrid":
        return keys
    if mode == "local":
        kept = [k for k in keys if meta.get(k, {}).get("is_local")]
    else:  # cloud
        kept = [k for k in keys if not meta.get(k, {}).get("is_local")]
    return kept or keys
