"""Speaker embedding encoders, behind one call.

Two encoders share one contract (float32 mono 16 kHz -> L2-normalized
vector), selected by the ``speaker_embedder`` config key:

ReDimNet2-B2 (default)
    0.57% EER on VoxCeleb1-O against ECAPA-VoxCeleb's ~1%, and the gap
    widens on the windows this app actually feeds it: the turn splitter
    embeds ~1.25 s slices, well under the ~3 s ECAPA was trained for.
    3.6M params, 15 MB checkpoint, MIT licensed, loaded through torch.hub.

ECAPA-VoxCeleb (fallback)
    What shipped before. Kept as the automatic fallback because ReDimNet2
    arrives over the network on first use: a blocked or offline first run
    must degrade to a working encoder, not to no speaker labels at all.

Both happen to emit 192 dimensions, but vectors from different encoders
are NOT comparable. Anything that persists an embedding must record which
encoder produced it (see voiceprints.py) and refuse cross-encoder matches.

Batch encoding matters here: the turn splitter embeds 10-25 windows per
utterance, and one padded forward pass is several times cheaper than a
loop of single-window calls.
"""

from __future__ import annotations

import logging
import os
import threading

import numpy as np

from server.infrastructure.paths import models_root

log = logging.getLogger("whisper-studio")

NAME_REDIMNET = "redimnet"
NAME_ECAPA = "ecapa"

MODELS_DIR = models_root()
ECAPA_MODEL_DIR = os.path.join(MODELS_DIR, "spkrec-ecapa-voxceleb")
ECAPA_REPO_ID = "speechbrain/spkrec-ecapa-voxceleb"

# ReDimNet2 variant. b2 is the accuracy/compute knee: 3.6M params and 0.95
# GMACs for 0.57% EER. b3/b4 buy ~0.15% more at 3-5x the compute, which is
# not worth it when the encoder runs on every utterance AND every turn
# window of a live meeting.
REDIMNET_HUB = "PalabraAI/redimnet2"
# torch.hub caches the repo and its weights here, under models/ with
# everything else the app downloads, rather than in ~/.cache.
REDIMNET_CACHE_DIR = os.path.join(MODELS_DIR, "torch-hub")
REDIMNET_VARIANT = "b2"
# The checkpoint torch.hub drops inside the cache dir; its presence is what
# the Settings model card checks for "Installed" (see models_manager).
REDIMNET_SENTINEL = os.path.join("checkpoints", f"{REDIMNET_VARIANT}-vox2-lm.pt")

# Embeddings shorter than this are phoneme noise, not identity.
MIN_EMBED_SAMPLES = 16000  # 1.0 s @ 16 kHz

_encoder = None
_encoder_name: str | None = None
_lock = threading.Lock()


def configured_name() -> str:
    """Which encoder the config asks for (unknown values fall back to ReDimNet2)."""
    from server.infrastructure.config import get as config_get

    raw = str(config_get("speaker_embedder") or NAME_REDIMNET).strip().lower()
    return raw if raw in (NAME_REDIMNET, NAME_ECAPA) else NAME_REDIMNET


def active_name() -> str | None:
    """The encoder actually loaded, or None before the first embed.

    Not the same as ``configured_name()``: a ReDimNet2 load failure falls
    back to ECAPA, and callers that persist vectors need the truth.
    """
    return _encoder_name


def _load_redimnet():
    """ReDimNet2 via torch.hub, cached under models/ with the app's other weights."""
    import torch

    torch.hub.set_dir(REDIMNET_CACHE_DIR)
    model = torch.hub.load(
        REDIMNET_HUB,
        "redimnet2",
        model_name=REDIMNET_VARIANT,
        train_type="lm",
        pretrained=True,
        trust_repo=True,
    )
    model.eval()
    return model


def _ensure_redimnet_files() -> str:
    """Download the ReDimNet2 repo and checkpoint if absent (idempotent).

    torch.hub has no download-only call, so this loads the model once and
    drops it; at 15 MB the transient load is trivial, and the files stay
    in REDIMNET_CACHE_DIR for the real load later. Gives the Settings
    model card the same blocking ensure function every other entry has.
    """
    _load_redimnet()
    return REDIMNET_CACHE_DIR


def _ensure_ecapa_files() -> str:
    """Download the ECAPA encoder into models/ if absent (idempotent)."""
    hyperparams = os.path.join(ECAPA_MODEL_DIR, "hyperparams.yaml")
    if not os.path.exists(hyperparams):
        from huggingface_hub import snapshot_download

        log.info("Downloading Speaker ID model %s ...", ECAPA_REPO_ID)
        snapshot_download(
            repo_id=ECAPA_REPO_ID,
            local_dir=ECAPA_MODEL_DIR,
            local_dir_use_symlinks=False,
        )
        log.info("Speaker ID model download complete.")
    return os.path.abspath(ECAPA_MODEL_DIR)


def _load_ecapa():
    from speechbrain.inference.speaker import EncoderClassifier

    _ensure_ecapa_files()
    return EncoderClassifier.from_hparams(
        source=os.path.abspath(ECAPA_MODEL_DIR),
        savedir=os.path.abspath(ECAPA_MODEL_DIR),
    )


def _get_encoder():
    """Lazily load the configured encoder, falling back to ECAPA."""
    global _encoder, _encoder_name
    if _encoder is not None:
        return _encoder
    with _lock:
        if _encoder is not None:
            return _encoder
        want = configured_name()
        if want == NAME_REDIMNET:
            try:
                log.info("Loading Speaker ID model (ReDimNet2-%s)...", REDIMNET_VARIANT)
                _encoder = _load_redimnet()
                _encoder_name = NAME_REDIMNET
                log.info("Speaker ID model loaded (ReDimNet2-%s).", REDIMNET_VARIANT)
                return _encoder
            except Exception as e:
                # Offline first run, torch.hub blocked, upstream repo moved.
                # A worse encoder beats no speaker labels.
                log.warning("ReDimNet2 unavailable (%s); falling back to ECAPA.", e)
        log.info("Loading Speaker ID model (ECAPA)...")
        _encoder = _load_ecapa()
        _encoder_name = NAME_ECAPA
        log.info("Speaker ID model loaded (ECAPA).")
        return _encoder


def reset() -> None:
    """Drop the loaded encoder so the next embed re-reads the config (tests, config change)."""
    global _encoder, _encoder_name
    with _lock:
        _encoder = None
        _encoder_name = None


def preload() -> None:
    """Eager load for startup warmup. Blocks."""
    _get_encoder()


def _forward(batch: np.ndarray) -> np.ndarray:
    """(B, T) float32 -> (B, D) raw embeddings on whichever encoder is loaded."""
    import torch

    model = _get_encoder()
    signal = torch.from_numpy(batch)
    with torch.no_grad():
        if _encoder_name == NAME_REDIMNET:
            out = model(signal)
        else:
            out = model.encode_batch(signal)
    return out.reshape(batch.shape[0], -1).cpu().numpy()


def _normalize(vec: np.ndarray) -> np.ndarray | None:
    norm = float(np.linalg.norm(vec))
    if norm == 0.0 or not np.isfinite(norm):
        return None
    return (vec / norm).astype(np.float32)


def encode(audio: np.ndarray | None) -> np.ndarray | None:
    """One window -> L2-normalized embedding, or None if it is too short to mean anything."""
    if audio is None or len(audio) < MIN_EMBED_SAMPLES:
        return None
    try:
        return _normalize(_forward(audio.astype(np.float32)[None, :])[0])
    except Exception as e:
        log.warning("Speaker embedding failed: %s", e)
        return None


def encode_batch(windows: list[np.ndarray]) -> list[np.ndarray | None]:
    """Many windows -> embeddings, in one padded forward pass.

    Windows under the length floor keep their slot as None rather than
    shifting the results, so callers can zip against their own list.
    Zero-padding to the longest window is safe for both encoders (they
    pool over time), and windows here differ by at most a hop anyway.
    """
    usable = [i for i, w in enumerate(windows) if w is not None and len(w) >= MIN_EMBED_SAMPLES]
    out: list[np.ndarray | None] = [None] * len(windows)
    if not usable:
        return out
    width = max(len(windows[i]) for i in usable)
    batch = np.zeros((len(usable), width), dtype=np.float32)
    for row, i in enumerate(usable):
        w = windows[i]
        batch[row, : len(w)] = w
    try:
        vectors = _forward(batch)
    except Exception as e:
        log.warning("Speaker embedding batch failed: %s", e)
        return out
    for row, i in enumerate(usable):
        out[i] = _normalize(vectors[row])
    return out
