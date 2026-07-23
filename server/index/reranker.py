"""Qwen3-Reranker-0.6B cross-encoder reranker (optional, behind ``rag_reranker``).

A cross-encoder judges each (query, passage) pair directly, so it reorders the
fused candidate pool with higher precision than dense+keyword ranking alone.
Qwen3-Reranker is LLM-based: it answers "yes"/"no" to whether a document meets
the query, and the relevance score is P("yes") from the final-token logits.

Same lazy-load / ``unload()`` pattern as the embedder (loads on first use, stays
warm across chat turns, freed on demand). Same family as the embedder, so it's
multilingual. Best-effort: any failure returns no scores and the caller keeps
the existing fused order.
"""

from __future__ import annotations

import logging
import threading

from .config import (
    COHERE_RERANK_MODEL_ID,
    RERANK_BATCH,
    RERANK_MAX_TOKENS,
    RERANK_MODEL,
    RERANK_MODEL_DIR,
    RERANK_SENTINEL,
)

log = logging.getLogger("whisper-studio")


def _rerank_backend() -> str:
    try:
        from server.infrastructure.model_mode import resolve_backend

        return resolve_backend("rerank")
    except Exception:
        return "qwen3"


def _rerank_cohere(query: str, passages: list[str]) -> list[float]:
    """Cohere Rerank 3.5 on Bedrock (us-east-1). Returns a relevance score per
    passage aligned to input order; [] on any failure so the caller keeps the
    fused order."""
    try:
        import json

        from . import embedder_cohere

        client = embedder_cohere._bedrock()
        body = json.dumps(
            {
                "query": query,
                "documents": [p or " " for p in passages],
                "top_n": len(passages),
                "api_version": 2,
            }
        )
        resp = client.invoke_model(modelId=COHERE_RERANK_MODEL_ID, body=body)
        payload = json.loads(resp["body"].read())
        scores = [0.0] * len(passages)
        for r in payload.get("results", []):
            idx = r.get("index")
            if isinstance(idx, int) and 0 <= idx < len(passages):
                scores[idx] = float(r.get("relevance_score", 0.0))
        return scores
    except Exception as e:  # noqa: BLE001 — reranking is best-effort
        log.warning("Cohere rerank failed: %s", e)
        return []


_model = None
_tokenizer = None
_device = None
_yes_id = None
_no_id = None
_prefix_ids: list[int] = []
_suffix_ids: list[int] = []
_lock = threading.Lock()

_INSTRUCTION = "Given a search query, retrieve the passages from the user's files most relevant to answering it"
_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based "
    "on the Query and the Instruct provided. Note that the answer can only be "
    '"yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def ensure_rerank_model() -> str:
    """Download Qwen3-Reranker into ./models if absent (idempotent, download-only).
    Called from setup.sh alongside the other model pulls."""
    import os

    if not os.path.exists(RERANK_SENTINEL):
        from huggingface_hub import snapshot_download

        log.info("Downloading reranker model %s ...", RERANK_MODEL)
        snapshot_download(
            repo_id=RERANK_MODEL, local_dir=RERANK_MODEL_DIR, local_dir_use_symlinks=False
        )
        log.info("Reranker model download complete.")
    return RERANK_MODEL_DIR


def is_downloaded() -> bool:
    import os

    return os.path.exists(RERANK_SENTINEL)


def is_loaded() -> bool:
    return _model is not None


def _load():
    global _model, _tokenizer, _device, _yes_id, _no_id, _prefix_ids, _suffix_ids
    if _model is not None:
        return
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    ensure_rerank_model()
    _device = "mps" if torch.backends.mps.is_available() else "cpu"
    log.info("Loading reranker model on %s ...", _device)
    # Left padding so the final-token logits line up at index -1 for every row.
    _tokenizer = AutoTokenizer.from_pretrained(RERANK_MODEL_DIR, padding_side="left")
    _model = AutoModelForCausalLM.from_pretrained(RERANK_MODEL_DIR, dtype=torch.float32)
    _model.eval()
    _model.to(_device)
    _yes_id = _tokenizer.convert_tokens_to_ids("yes")
    _no_id = _tokenizer.convert_tokens_to_ids("no")
    # Pre-tokenize the fixed prefix/suffix once. They wrap the (truncated) middle
    # at scoring time so the SUFFIX — the assistant turn whose last-token logits
    # carry the yes/no answer — is NEVER truncated away (the official recipe).
    _prefix_ids = _tokenizer.encode(_PREFIX, add_special_tokens=False)
    _suffix_ids = _tokenizer.encode(_SUFFIX, add_special_tokens=False)
    log.info("Reranker model loaded.")


def unload() -> None:
    """Free the reranker weights (release RAM when idle / before an index run)."""
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
            log.debug("Reranker mps cache clear failed: %s", e)
        import gc

        gc.collect()
        log.info("Reranker model unloaded.")


def _middle_ids(query: str, doc: str, budget: int) -> list[int]:
    """Tokenize the variable middle (instruct/query/document), truncated to the
    budget left after the fixed prefix+suffix — so wrapping never truncates the
    suffix the model scores on."""
    mid = f"<Instruct>: {_INSTRUCTION}\n<Query>: {query}\n<Document>: {doc}"
    return _tokenizer.encode(
        mid, add_special_tokens=False, truncation=True, max_length=max(8, budget)
    )


def rerank(query: str, passages: list[str], backend: str | None = None) -> list[float]:
    """Relevance score for each passage given the query, aligned to input order.
    Routes to the active rerank backend (on-device Qwen3-Reranker, or Cohere
    Rerank 3.5 on Bedrock in cloud mode). Returns [] on any failure so the
    caller keeps its existing fused order."""
    if not passages:
        return []
    if (backend or _rerank_backend()) == "cohere":
        return _rerank_cohere(query, passages)
    try:
        import torch

        with _lock:
            _load()
            budget = RERANK_MAX_TOKENS - len(_prefix_ids) - len(_suffix_ids)
            scores: list[float] = []
            for i in range(0, len(passages), RERANK_BATCH):
                batch = passages[i : i + RERANK_BATCH]
                ids = [
                    _prefix_ids + _middle_ids(query, p or "", budget) + _suffix_ids for p in batch
                ]
                enc = _tokenizer.pad({"input_ids": ids}, padding=True, return_tensors="pt").to(
                    _device
                )
                with torch.no_grad():
                    logits = _model(**enc).logits[:, -1, :]  # last (real) token, left-padded
                pair = torch.stack([logits[:, _no_id], logits[:, _yes_id]], dim=1)
                p_yes = torch.softmax(pair, dim=1)[:, 1]
                scores.extend(p_yes.cpu().to(torch.float32).tolist())
            return scores
    except Exception as e:  # noqa: BLE001 — reranking is best-effort
        log.warning("Rerank failed: %s", e)
        return []
