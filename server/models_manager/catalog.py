"""The managed-model catalog: every downloadable model, with live disk state.

One entry per model the app can download on demand — ASR/diarization weights,
the workspace-index models, and the on-device chat GGUFs. Each entry records
where the model lives under ``models_root()`` and which file marks it as
installed, REUSING the layout the owning module's ensure_* function checks
(same directory, same sentinel file) rather than inventing a parallel check.

Paths are re-rooted at ``models_root()`` on every query instead of read from
the owning modules' frozen ``*_MODEL_DIR`` constants: those are resolved once
at import time, so they go stale when ``WHISPER_HOME`` changes afterwards
(tests, packaged-app relaunch). The *relative* layout — directory name and
sentinel filename — is taken from the modules themselves, so the check cannot
drift from what the ensure functions actually download.

The static entries are built lazily (the ASR modules import numpy) and cached;
local-chat entries are re-read from the config-driven registry on every call so
a model added to config.json shows up without a restart.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

from server.infrastructure.paths import models_root

log = logging.getLogger("whisper-studio")

GROUP_TRANSCRIPTION = "transcription"
GROUP_INDEXING = "indexing"
GROUP_LOCAL_CHAT = "local-chat"


@dataclass(frozen=True)
class ModelEntry:
    key: str
    label: str
    group: str  # transcription | indexing | local-chat
    repo_id: str
    dir_name: str  # directory under models_root()
    sentinel_rel: str  # file (relative to the dir) whose existence == installed
    ensure_module: str  # dotted module holding the blocking download function
    ensure_func: str
    ensure_arg: str | None = None  # single positional arg (registry key for GGUFs)
    gguf_filename: str | None = None  # local-chat: the one target file in the repo
    # One-line "what is this good for" shown on the model's card in Settings.
    note: str = ""


@lru_cache(maxsize=1)
def _static_entries() -> tuple[ModelEntry, ...]:
    """Transcription + indexing entries, derived from the owning modules.

    ``dir_name``/``sentinel_rel`` are computed from the modules' own path
    constants (relative to their MODELS_DIR) so the catalog checks exactly the
    files their ensure functions download. The three ASR sentinels are local
    variables inside the ensure functions, so their basenames are mirrored here
    with a pointer to the source.
    """
    from server.asr import canary_backend, parakeet_backend, whisper_backend
    from server.diarization import speakers
    from server.index import config as ic

    def rel(path: str, root: str) -> str:
        return os.path.relpath(path, root)

    return (
        ModelEntry(
            key="whisper",
            label="Whisper Large v3 Turbo",
            group=GROUP_TRANSCRIPTION,
            repo_id=whisper_backend.WHISPER_REPO_ID,
            dir_name=rel(whisper_backend.WHISPER_MODEL_DIR, whisper_backend.MODELS_DIR),
            # Mirrors whisper_backend._ensure_model's weight_file check.
            sentinel_rel="weights.safetensors",
            ensure_module="server.asr.whisper_backend",
            ensure_func="_ensure_turbo",
            note=(
                "Fast, accurate transcription in 99 languages; the default "
                "engine. Translation to English uses token steering (decent, "
                "not its strength)."
            ),
        ),
        ModelEntry(
            key="whisper_large_v3",
            label="Whisper Large v3 (full)",
            group=GROUP_TRANSCRIPTION,
            repo_id="mlx-community/whisper-large-v3-mlx",
            dir_name="whisper-large-v3",
            # This MLX repo ships npz weights, not safetensors.
            sentinel_rel="weights.npz",
            ensure_module="server.asr.whisper_backend",
            ensure_func="_ensure_large_v3",
            note=(
                "Highest transcription accuracy and a true translate-to-"
                "English mode. 2x the size of Turbo and sentences appear "
                "1-2 s later. Pick it in Settings as the Whisper variant."
            ),
        ),
        ModelEntry(
            key="canary",
            label="Canary 1B v2",
            group=GROUP_TRANSCRIPTION,
            repo_id=canary_backend.CANARY_REPO_ID,
            dir_name=rel(canary_backend.CANARY_MODEL_DIR, canary_backend.MODELS_DIR),
            # Mirrors canary_backend._ensure_model's weight_file check.
            sentinel_rel="model.safetensors",
            ensure_module="server.asr.canary_backend",
            ensure_func="_ensure_model",
            note=(
                "Best speech translation to English plus very fast, accurate "
                "transcription for 25 European languages. No language "
                "auto-detect: set your session language in Settings, so it "
                "suits single-language meetings."
            ),
        ),
        ModelEntry(
            key="parakeet",
            label="Parakeet TDT 0.6B v3",
            group=GROUP_TRANSCRIPTION,
            repo_id=parakeet_backend.PARAKEET_REPO_ID,
            dir_name=rel(parakeet_backend.PARAKEET_MODEL_DIR, parakeet_backend.MODELS_DIR),
            # Mirrors parakeet_backend._ensure_parakeet_model's weight_file check.
            sentinel_rel="model.safetensors",
            ensure_module="server.asr.parakeet_backend",
            ensure_func="_ensure_parakeet_model",
            note=(
                "Lowest latency: words appear live as you speak, in 25 "
                "European languages. No translation support."
            ),
        ),
        ModelEntry(
            key="ecapa_speaker",
            label="ECAPA Speaker Encoder",
            group=GROUP_TRANSCRIPTION,
            repo_id=speakers.SPEAKER_REPO_ID,
            dir_name=rel(speakers.SPEAKER_MODEL_DIR, speakers.MODELS_DIR),
            # Mirrors speakers._ensure_speaker_model's hyperparams check.
            sentinel_rel="hyperparams.yaml",
            ensure_module="server.diarization.speakers",
            ensure_func="_ensure_speaker_model",
        ),
        ModelEntry(
            key="qwen3_embed",
            label="Qwen3 Embedding 0.6B",
            group=GROUP_INDEXING,
            repo_id=ic.EMBED_MODEL,
            dir_name=rel(ic.EMBED_MODEL_DIR, ic.MODELS_DIR),
            sentinel_rel=rel(ic.EMBED_SENTINEL, ic.EMBED_MODEL_DIR),
            ensure_module="server.index.embedder",
            ensure_func="ensure_embed_model",
        ),
        ModelEntry(
            key="qwen3_rerank",
            label="Qwen3 Reranker 0.6B",
            group=GROUP_INDEXING,
            repo_id=ic.RERANK_MODEL,
            dir_name=rel(ic.RERANK_MODEL_DIR, ic.MODELS_DIR),
            sentinel_rel=rel(ic.RERANK_SENTINEL, ic.RERANK_MODEL_DIR),
            ensure_module="server.index.reranker",
            ensure_func="ensure_rerank_model",
        ),
        ModelEntry(
            key="gliner",
            label="GLiNER Large v2.5",
            group=GROUP_INDEXING,
            repo_id=ic.GLINER_MODEL,
            dir_name=rel(ic.GLINER_MODEL_DIR, ic.MODELS_DIR),
            sentinel_rel=rel(ic.GLINER_SENTINEL, ic.GLINER_MODEL_DIR),
            # ensure_gliner_model also pre-caches the backbone tokenizer, so
            # the first extraction after a download works offline. Do not swap
            # this for a bare snapshot_download.
            ensure_module="server.index.extractor",
            ensure_func="ensure_gliner_model",
        ),
        ModelEntry(
            key="gliner2",
            label="GLiNER2 Large v1",
            group=GROUP_INDEXING,
            repo_id=ic.GLINER2_MODEL,
            dir_name=rel(ic.GLINER2_MODEL_DIR, ic.MODELS_DIR),
            sentinel_rel=rel(ic.GLINER2_SENTINEL, ic.GLINER2_MODEL_DIR),
            ensure_module="server.index.extractor",
            ensure_func="ensure_gliner2_model",
        ),
    )


def _local_chat_entries() -> list[ModelEntry]:
    """One entry per on-device chat model in the config-driven registry.

    Re-read on every call (the registry is a live built-ins + config merge).
    The GGUF file itself is the sentinel — the same check as
    ``server.local.runtime.is_downloaded``.
    """
    from server.local.registry import local_models

    static_keys = {e.key for e in _static_entries()}
    out: list[ModelEntry] = []
    for key, m in local_models().items():
        if key in static_keys:  # defensive: a config key must not shadow a built-in entry
            log.warning("Local model key %r collides with a catalog entry; skipping.", key)
            continue
        out.append(
            ModelEntry(
                key=key,
                label=m.get("label") or key,
                group=GROUP_LOCAL_CHAT,
                repo_id=m["repo_id"],
                dir_name=m["dir"],
                sentinel_rel=m["filename"],
                ensure_module="server.local.runtime",
                ensure_func="ensure_downloaded",
                ensure_arg=key,
                gguf_filename=m["filename"],
            )
        )
    return out


def entries() -> list[ModelEntry]:
    return list(_static_entries()) + _local_chat_entries()


def get_entry(key: str) -> ModelEntry | None:
    for e in entries():
        if e.key == key:
            return e
    return None


def model_dir(entry: ModelEntry) -> str:
    return os.path.join(models_root(), entry.dir_name)


def is_installed(entry: ModelEntry) -> bool:
    return os.path.exists(os.path.join(model_dir(entry), entry.sentinel_rel))


def bytes_on_disk(entry: ModelEntry) -> int:
    """Total bytes under the model's directory — including hf's .incomplete
    files, so an in-flight download's progress is visible as tree growth."""
    root = model_dir(entry)
    if not os.path.isdir(root):
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total
