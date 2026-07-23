"""Qwen3-Embedding-0.6B runner — text → 1024-d L2-normalized vectors.

Runs on transformers + torch (MPS on Apple Silicon, CPU fallback), the same
runtime GLiNER uses, so no extra ML stack is pulled in. Loading is lazy and the
weights can be freed with ``unload()`` — the same load-on-demand / free-on-idle
pattern the ASR backends use, so an index run doesn't hold ~1 GB resident
alongside the chat LLM.

Pooling follows the Qwen3-Embedding recipe: left-pad, take the last token's
hidden state, L2-normalize. Queries get an instruction prefix; documents do not.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from .config import (
    EMBED_BATCH,
    EMBED_DIM,
    EMBED_MAX_TOKENS,
    EMBED_MODEL,
    EMBED_MODEL_DIR,
    EMBED_SENTINEL,
    QUERY_INSTRUCTION,
)

log = logging.getLogger("whisper-studio")


def _embed_backend() -> str:
    """The active embed backend for index/query. Defaults to qwen3 if the mode
    resolver is unavailable (e.g. early import)."""
    try:
        from server.infrastructure.model_mode import resolve_backend

        return resolve_backend("embed")
    except Exception:
        return "qwen3"


_model = None
_tokenizer = None
_device = None
_lock = threading.Lock()


def ensure_embed_model() -> str:
    """Download Qwen3-Embedding into ./models if absent (idempotent, download-only)."""
    import os

    if not os.path.exists(EMBED_SENTINEL):
        from huggingface_hub import snapshot_download

        log.info("Downloading embedding model %s ...", EMBED_MODEL)
        snapshot_download(
            repo_id=EMBED_MODEL, local_dir=EMBED_MODEL_DIR, local_dir_use_symlinks=False
        )
        log.info("Embedding model download complete.")
    return EMBED_MODEL_DIR


def _load():
    global _model, _tokenizer, _device
    if _model is not None:
        return
    import torch
    from transformers import AutoModel, AutoTokenizer

    ensure_embed_model()
    _device = "mps" if torch.backends.mps.is_available() else "cpu"
    log.info("Loading embedding model on %s ...", _device)
    _tokenizer = AutoTokenizer.from_pretrained(EMBED_MODEL_DIR, padding_side="left")
    _model = AutoModel.from_pretrained(EMBED_MODEL_DIR, dtype=torch.float32)
    _model.eval()
    _model.to(_device)
    log.info("Embedding model loaded.")


def unload() -> None:
    """Free the embedding weights (call after an index run to release RAM)."""
    global _model, _tokenizer
    with _lock:
        if _model is None:
            return
        _model = None
        _tokenizer = None
        try:
            import torch

            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception as e:
            log.debug("Embedding mps cache clear failed: %s", e)
        import gc

        gc.collect()
        log.info("Embedding model unloaded.")


def is_loaded() -> bool:
    return _model is not None


def _last_token_pool(last_hidden, attn_mask):
    # Left padding -> the real last token is at index -1 for every row.
    left = attn_mask[:, -1].sum() == attn_mask.shape[0]
    if left:
        return last_hidden[:, -1]
    import torch

    idx = attn_mask.sum(dim=1) - 1
    return last_hidden[torch.arange(last_hidden.size(0)), idx]


def _forward_batch(batch: list[str]) -> np.ndarray:
    """One forward pass returning (n, EMBED_DIM) float32: tokenize, run the model,
    pool the last token, then unit-length normalize each row. Caller must hold
    ``_lock`` and have called ``_load()``."""
    import torch

    enc = _tokenizer(
        batch, padding=True, truncation=True, max_length=EMBED_MAX_TOKENS, return_tensors="pt"
    ).to(_device)
    with torch.no_grad():
        res = _model(**enc)
    vec = _last_token_pool(res.last_hidden_state, enc["attention_mask"])
    vec = torch.nn.functional.normalize(vec, p=2, dim=1)
    return vec.cpu().to(torch.float32).numpy()


def _embed(texts: list[str]) -> np.ndarray:
    """Embed a list of strings -> (n, EMBED_DIM) float32, L2-normalized.

    Guards against the intermittent non-finite (NaN/inf) vectors the MPS path can
    emit: any bad row is recomputed (the glitch is transient, so a retry almost
    always clears it) and zeroed only as a last resort. A persisted NaN vector
    silently makes a document unfindable for every query AND breaks sqlite-vec,
    so this never returns one."""
    with _lock:
        _load()
        out_vecs: list[np.ndarray] = []
        for i in range(0, len(texts), EMBED_BATCH):
            out_vecs.append(_forward_batch(texts[i : i + EMBED_BATCH]))
        arr = np.vstack(out_vecs) if out_vecs else np.zeros((0, EMBED_DIM), dtype=np.float32)
        if arr.size:
            for _ in range(3):
                bad = ~np.isfinite(arr).all(axis=1)
                if not bad.any():
                    break
                log.warning(
                    "Embedding produced %d non-finite vector(s); recomputing", int(bad.sum())
                )
                for j in np.nonzero(bad)[0]:
                    arr[j] = _forward_batch([texts[j]])[0]
            bad = ~np.isfinite(arr).all(axis=1)
            if bad.any():
                log.error(
                    "Embedding still non-finite for %d text(s) after retries; zeroing",
                    int(bad.sum()),
                )
                arr[bad] = 0.0
        return arr


def embed_documents(texts: list[str]) -> np.ndarray:
    """Embed passages for indexing. Routes to the active embed backend
    (on-device Qwen3, or Cohere Embed v4 on Bedrock in cloud mode)."""
    if _embed_backend() == "cohere":
        from . import embedder_cohere

        return embedder_cohere.embed_documents(texts)
    if not texts:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)
    return _embed(texts)


def embed_query(text: str) -> np.ndarray:
    """Embed a search query. Routes to the active embed backend. Qwen3 gets an
    instruction prefix; Cohere uses input_type=search_query instead."""
    if _embed_backend() == "cohere":
        from . import embedder_cohere

        return embedder_cohere.embed_query(text)
    return _embed([QUERY_INSTRUCTION + text])[0]
