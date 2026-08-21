"""Download-size estimates for catalog entries.

Real numbers come from the HF Hub API (per-file sizes via
``HfApi().model_info(repo_id, files_metadata=True)``), fetched lazily in a
background thread the first time the catalog is requested and cached in memory
for the life of the process. Until a fetch lands — or forever, when offline —
the hardcoded fallback table below answers instead, so the UI always has a
plausible size to show. GGUF entries count only their one target file; every
other entry sums the whole repo.
"""

from __future__ import annotations

import logging
import threading

from server.models_manager.catalog import ModelEntry

log = logging.getLogger("whisper-studio")

# Measured from real downloads (exact bytes on disk, 2026-08). Estimates only:
# used when the HF API is unreachable or hasn't answered yet. Keyed by catalog
# key; a config-added local model without an entry here shows no size until the
# API answers.
FALLBACK_SIZES: dict[str, int] = {
    "whisper_large_v3": 3_086_000_000,
    "canary": 2_460_000_000,
    "parakeet": 2_509_045_082,
    "ecapa_speaker": 89_134_981,
    "qwen3_embed": 1_207_490_483,
    "qwen3_rerank": 1_207_489_987,
    "gliner": 3_687_509_485,
    "gliner2": 1_961_370_346,
    "local_gemma": 6_975_879_296,
    "local_gemma_coder": 7_381_381_664,
    "local_qwen35_9b": 5_680_522_464,
    "local_ministral3_14b": 8_366_206_144,
}

_cache: dict[str, int] = {}  # cache key -> real size from the HF API
_attempted: set[str] = set()  # fetched (or failed) once already — don't retry
_lock = threading.Lock()


def _cache_key(entry: ModelEntry) -> str:
    return f"{entry.repo_id}::{entry.gguf_filename or ''}"


def fetch_size(repo_id: str, filename: str | None = None) -> int | None:
    """Sum of per-file sizes for a repo (or one file of it) from the HF API.
    Network-bound and blocking; returns None on any failure."""
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo_id, files_metadata=True)
    except Exception as e:  # offline, gated repo, bad id — all non-fatal
        log.debug("HF size fetch failed for %s: %s", repo_id, e)
        return None
    total = 0
    for sibling in getattr(info, "siblings", None) or []:
        size = getattr(sibling, "size", None)
        if not size:
            continue
        if filename is not None and sibling.rfilename != filename:
            continue
        total += int(size)
    return total or None


def estimate(entry: ModelEntry) -> int | None:
    """Best size estimate right now: the fetched real size, else the fallback."""
    with _lock:
        cached = _cache.get(_cache_key(entry))
    return cached if cached is not None else FALLBACK_SIZES.get(entry.key)


def prefetch(entries: list[ModelEntry]) -> None:
    """Kick a background fetch of real sizes for entries not yet attempted.

    Fire-and-forget: callers keep serving fallback estimates until the thread
    fills the cache. One attempt per repo per process — a failure (offline)
    leaves the fallback in charge rather than re-blocking on every poll.
    """
    todo: list[tuple[str, str, str | None]] = []
    with _lock:
        for e in entries:
            ck = _cache_key(e)
            if ck in _attempted:
                continue
            _attempted.add(ck)
            todo.append((ck, e.repo_id, e.gguf_filename))
    if not todo:
        return

    def _run() -> None:
        for ck, repo_id, filename in todo:
            size = fetch_size(repo_id, filename)
            if size:
                with _lock:
                    _cache[ck] = size

    threading.Thread(target=_run, name="models-size-fetch", daemon=True).start()
