"""Cohere Embed v4 backend (Amazon Bedrock) — text → 1536-d L2-normalized vectors.

The cloud counterpart to the on-device Qwen3 embedder. Called via bedrock-runtime
InvokeModel against ``cohere.embed-v4:0``, region-pinned to us-east-1 (where Embed
v4 + Rerank 3.5 live). Cohere uses an asymmetric recipe: documents are embedded
with ``input_type=search_document`` and queries with ``search_query``. Cohere
vectors are not unit-length, so we L2-normalize each row to match the store's
cosine search (dot product on unit vectors).

No weights are downloaded or held resident — it's a network call — so unload()/
is_loaded() are no-ops kept for API parity with the on-device embedder.
"""

from __future__ import annotations

import json
import logging
import threading

import numpy as np

from .config import COHERE_EMBED_DIM, COHERE_EMBED_MODEL_ID, COHERE_REGION

log = logging.getLogger("whisper-studio")

# Cohere caps a single embed request at 96 texts.
_MAX_TEXTS_PER_CALL = 96

_client = None
_client_lock = threading.Lock()


def _bedrock():
    """A us-east-1 bedrock-runtime client (Cohere embed/rerank are only there),
    independent of the chat region. Cached process-wide."""
    global _client
    with _client_lock:
        if _client is None:
            import boto3
            from botocore.config import Config as BotoConfig

            _client = boto3.client(
                "bedrock-runtime",
                region_name=COHERE_REGION,
                config=BotoConfig(
                    read_timeout=120,
                    connect_timeout=10,
                    retries={"max_attempts": 3},
                    max_pool_connections=16,
                ),
            )
        return _client


def _l2(arr: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (arr / norms).astype(np.float32)


def _invoke(texts: list[str], input_type: str) -> np.ndarray:
    """Embed texts via Cohere Embed v4, returning (n, dim) L2-normalized float32.
    Raises on failure — a missing/garbage vector silently breaks retrieval, so a
    build must fail loudly rather than persist one."""
    client = _bedrock()
    out: list[list[float]] = []
    for i in range(0, len(texts), _MAX_TEXTS_PER_CALL):
        chunk = [t if (t and t.strip()) else " " for t in texts[i : i + _MAX_TEXTS_PER_CALL]]
        body = json.dumps(
            {
                "texts": chunk,
                "input_type": input_type,
                "embedding_types": ["float"],
                "output_dimension": COHERE_EMBED_DIM,
            }
        )
        resp = client.invoke_model(modelId=COHERE_EMBED_MODEL_ID, body=body)
        payload = json.loads(resp["body"].read())
        emb = payload.get("embeddings")
        # v4 with embedding_types returns {"float": [[...]]}; tolerate a bare list.
        rows = emb.get("float") if isinstance(emb, dict) else emb
        if not rows or len(rows) != len(chunk):
            raise RuntimeError(f"Cohere embed returned {len(rows or [])} of {len(chunk)} vectors")
        out.extend(rows)
    arr = np.asarray(out, dtype=np.float32)
    if arr.size and not np.isfinite(arr).all():
        raise RuntimeError("Cohere embed returned non-finite values")
    return _l2(arr) if arr.size else np.zeros((0, COHERE_EMBED_DIM), dtype=np.float32)


def embed_documents(texts: list[str]) -> np.ndarray:
    """Embed passages for indexing (search_document) -> (n, dim) float32."""
    if not texts:
        return np.zeros((0, COHERE_EMBED_DIM), dtype=np.float32)
    return _invoke(texts, "search_document")


def embed_query(text: str) -> np.ndarray:
    """Embed a search query (search_query) -> (dim,) float32."""
    return _invoke([text], "search_query")[0]


def unload() -> None:
    """No-op: nothing is resident (Bedrock is a network call)."""
    return


def is_loaded() -> bool:
    return False
