"""Named voiceprints that outlive the session.

Session-scoped labels answer "who spoke when" inside one recording and
reset to "Speaker 1" the next morning. This gallery answers the other
half: a person who spoke in an earlier recording is recognised in this
one and gets their name back.

Enrollment is a side effect of the rename the user already does. Clicking
"Speaker 2" and typing "Anna" stores the centroid of every embedding that
cluster has accumulated so far, so the gallery is built from real meeting
audio rather than from a separate enrollment ritual.

Matching is deliberately conservative. Putting the wrong name on somebody
is far worse than leaving them as "Speaker 3": a hit needs both a high
absolute similarity and a clear margin over the runner-up, or it is not a
hit at all.

Vectors from different encoders are not comparable, so entries are filed
per encoder (see embedder.py). Switching encoder hides the other
encoder's gallery instead of silently matching against nonsense.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import numpy as np

from server.diarization import embedder
from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

STORE_NAME = "voiceprints.json"
VERSION = 1

# Minimum cosine similarity to claim a stored identity, per encoder.
# ReDimNet2 separates speakers more sharply than ECAPA, so it can afford
# the stricter bar; ECAPA's real-world same-speaker similarities sit low
# enough (0.25-0.55 on short utterances) that 0.45 is already ambitious.
MATCH_MIN = {embedder.NAME_REDIMNET: 0.50, embedder.NAME_ECAPA: 0.45}
# ...and the winner must beat the runner-up by this much. Two similar
# voices that both sit near the threshold are better left unnamed.
MATCH_MARGIN = 0.06
# Enrolling from fewer embeddings than this makes a centroid that mostly
# encodes one utterance's acoustics rather than the person.
MIN_ENROLL_EMBEDDINGS = 2

_lock = threading.RLock()


def _store_path() -> str:
    return os.path.join(data_root(), STORE_NAME)


def enabled() -> bool:
    from server.infrastructure.config import get as config_get

    value = config_get("speaker_voiceprints")
    return True if value is None else bool(value)


def _load() -> dict:
    try:
        with open(_store_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("encoders"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Voiceprint store unreadable (%s); starting empty.", e)
    return {"version": VERSION, "encoders": {}}


def _save(data: dict) -> None:
    """Atomic write so a crash mid-save cannot truncate the gallery."""
    path = _store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _bucket(data: dict, encoder: str) -> list[dict]:
    return data["encoders"].setdefault(encoder, [])


def _active_encoder() -> str:
    """Which gallery to read/write.

    The loaded encoder decides it. If none is loaded we fall back to the
    configured one rather than loading it: reading the gallery must never
    pull a model into memory as a side effect, and in practice anything
    holding an embedding has already loaded an encoder.
    """
    return embedder.active_name() or embedder.configured_name()


def match(embedding: np.ndarray | None) -> str | None:
    """Name for this embedding, or None when nothing is a confident match."""
    if embedding is None or not enabled():
        return None
    encoder = _active_encoder()
    with _lock:
        entries = _bucket(_load(), encoder)
    if not entries:
        return None
    scored: list[tuple[float, str]] = []
    for entry in entries:
        centroid = np.asarray(entry.get("centroid", []), dtype=np.float32)
        if centroid.shape != embedding.shape:
            continue
        scored.append((float(centroid @ embedding), str(entry.get("name", ""))))
    if not scored:
        return None
    scored.sort(reverse=True)
    best_score, best_name = scored[0]
    if best_score < MATCH_MIN.get(encoder, 0.5) or not best_name:
        return None
    if len(scored) > 1 and best_score - scored[1][0] < MATCH_MARGIN:
        return None
    return best_name


def enroll(name: str, embeddings: list[np.ndarray]) -> bool:
    """Store (or strengthen) the voiceprint for ``name``.

    Re-enrolling an existing name folds the new embeddings into the
    running centroid weighted by how many each side represents, so a
    person's print keeps improving across meetings instead of being
    replaced by the latest one.
    """
    name = (name or "").strip()
    usable = [e for e in embeddings if e is not None]
    if not name or not enabled() or len(usable) < MIN_ENROLL_EMBEDDINGS:
        return False
    fresh = np.stack(usable).mean(axis=0)
    norm = float(np.linalg.norm(fresh))
    if norm == 0.0:
        return False
    fresh = fresh / norm
    encoder = _active_encoder()
    with _lock:
        data = _load()
        entries = _bucket(data, encoder)
        existing = next((e for e in entries if str(e.get("name", "")) == name), None)
        if existing is not None:
            prior = np.asarray(existing.get("centroid", []), dtype=np.float32)
            prior_n = int(existing.get("count", 1))
            if prior.shape == fresh.shape:
                merged = prior * prior_n + fresh * len(usable)
                merged_norm = float(np.linalg.norm(merged))
                if merged_norm > 0.0:
                    fresh = merged / merged_norm
            existing["centroid"] = [float(x) for x in fresh]
            existing["count"] = prior_n + len(usable)
            existing["updated"] = int(time.time())
        else:
            entries.append(
                {
                    "name": name,
                    "centroid": [float(x) for x in fresh],
                    "count": len(usable),
                    "updated": int(time.time()),
                }
            )
        _save(data)
    log.info("Voiceprint stored for %r (%s, %d embeddings).", name, encoder, len(usable))
    return True


def forget(name: str) -> bool:
    """Drop one stored voiceprint. Returns whether anything was removed."""
    name = (name or "").strip()
    if not name:
        return False
    encoder = _active_encoder()
    with _lock:
        data = _load()
        entries = _bucket(data, encoder)
        kept = [e for e in entries if str(e.get("name", "")) != name]
        if len(kept) == len(entries):
            return False
        data["encoders"][encoder] = kept
        _save(data)
    return True


def names() -> list[str]:
    """Every stored name for the active encoder, most recently updated first."""
    encoder = _active_encoder()
    with _lock:
        entries = _bucket(_load(), encoder)
    return [
        str(e.get("name", ""))
        for e in sorted(entries, key=lambda e: -int(e.get("updated", 0)))
        if e.get("name")
    ]
