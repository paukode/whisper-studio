"""Session-scoped speaker identification — assign fast, refine by re-clustering.

Backend-agnostic: consumes the float32 utterance audio that any ASR
backend attaches to its final events (see server/asr/base.py) and never
imports backend code. Labels are session-scoped ("Speaker 1" today and
"Speaker 1" tomorrow are unrelated) and live in RAM only: a reconnect
within the server's lifetime keeps its labels, a restart starts fresh.

Two-stage design:

1. ``assign()`` labels every utterance immediately via cosine similarity
   against running cluster centroids, so the transcript never waits.
   Margin rule: a clear match joins its cluster, a clear miss creates a
   new speaker, and the ambiguous band in between *joins the nearest
   cluster provisionally instead of spawning a new one* — borderline
   embeddings creating spurious speakers was the historical failure mode
   (74 "speakers" from 6 real ones in a 3 h session).

2. ``maybe_recluster()`` periodically re-runs agglomerative clustering
   over every embedding seen so far and returns label corrections for
   utterances whose assignment changed. Early mistakes (a noisy first
   centroid, a provisional join that turned out wrong) self-heal instead
   of compounding. The caller pushes the corrections to the client as a
   ``speaker_update`` message.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

log = logging.getLogger("whisper-studio")

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS_DIR = os.path.join(SCRIPT_DIR, "models")
SPEAKER_MODEL_DIR = os.path.join(MODELS_DIR, "spkrec-ecapa-voxceleb")
SPEAKER_REPO_ID = "speechbrain/spkrec-ecapa-voxceleb"

# ECAPA-VoxCeleb is trained on ~3 s utterances. Below ~1 s the embedding
# is dominated by phoneme content, not speaker identity. The upstream VAD
# buffer makes most utterances comfortably exceed this bar.
MIN_EMBED_SAMPLES = 16000  # 1.0 s @ 16 kHz
# Threshold calibration. ECAPA cosine similarities on short (1-8 s)
# real-world utterances run far lower than the folklore 0.6+: the same
# speaker across two utterances typically lands 0.25-0.55 (worse with
# room reverb, played-back media, or music underneath), while different
# speakers land roughly 0.0-0.2. The original 0.60/0.45 pair was tuned
# against close-mic audio and over-segmented badly in practice (11
# "speakers" from a 2-person clip). Lower if speakers get split, raise
# if they get merged.
#
# Fallback re-clustering cut (similarity): only used when the merge-gap
# heuristic in _agglomerative can't find a confident cluster count.
MATCH_THRESHOLD = 0.40
# Below this, the embedding is clearly nobody we know — create a new
# speaker. At or above it, join the best cluster (provisionally when
# weak) and let re-clustering correct any mistake. Deliberately low: the
# canonical online system (diart) tunes its equivalent to similarity
# ~0.0 on AMI meetings, because a wrong merge is one fixable label while
# a wrong new speaker pollutes everything after it. The re-clusterer
# owns the real cluster count.
NEW_SPEAKER_THRESHOLD = 0.15
# Utterances shorter than this never CREATE a new speaker — short
# windows give unreliable embeddings (a cough or cross-talk fragment
# must not define a new identity). They still get labels (nearest
# cluster) and re-clustering can still move them later. The same idea as
# diart's rho_update quality gate, applied to creation.
MIN_NEW_SPEAKER_SEC = 2.0
# Real meetings rarely exceed ~10 speakers; at the cap we snap to the
# nearest cluster instead of creating yet another spurious speaker.
MAX_SPEAKERS = 12
# Re-cluster after this many new finalized utterances...
RECLUSTER_EVERY = 10
# ...or this many when a brand-new speaker was just created — that is
# exactly when assignment mistakes happen, so don't sit on them.
RECLUSTER_AFTER_NEW = 3
# Tree cutting (cosine distance): if even the final merge is below this,
# everything is one speaker; and a merge-distance gap must be at least
# MIN_GAP to be trusted as the cluster-count boundary.
SINGLE_CLUSTER_DIST = 0.60
MIN_GAP = 0.12

# ECAPA embedding (torch/speechbrain) work runs here, off the event loop
# and off the backends' decode threads.
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="diarize")

_encoder = None
_encoder_lock = threading.Lock()


def _ensure_speaker_model() -> str:
    """Download the ECAPA speaker encoder into models/ if absent (idempotent, download-only)."""
    hyperparams = os.path.join(SPEAKER_MODEL_DIR, "hyperparams.yaml")
    if not os.path.exists(hyperparams):
        from huggingface_hub import snapshot_download

        log.info("Downloading Speaker ID model %s ...", SPEAKER_REPO_ID)
        snapshot_download(
            repo_id=SPEAKER_REPO_ID,
            local_dir=SPEAKER_MODEL_DIR,
            local_dir_use_symlinks=False,
        )
        log.info("Speaker ID model download complete.")
    return os.path.abspath(SPEAKER_MODEL_DIR)


def _get_encoder():
    """Lazily download + load the ECAPA speaker encoder."""
    global _encoder
    if _encoder is not None:
        return _encoder
    with _encoder_lock:
        if _encoder is None:
            _ensure_speaker_model()
            log.info("Loading Speaker ID model...")
            from speechbrain.inference.speaker import EncoderClassifier

            _encoder = EncoderClassifier.from_hparams(
                source=os.path.abspath(SPEAKER_MODEL_DIR),
                savedir=os.path.abspath(SPEAKER_MODEL_DIR),
            )
            log.info("Speaker ID model loaded.")
    return _encoder


def preload() -> None:
    """Eagerly load the encoder (startup warmup). Blocks until loaded."""
    executor.submit(_get_encoder).result()


def embed(audio: np.ndarray) -> np.ndarray | None:
    """Float32 mono 16 kHz audio -> L2-normalized speaker embedding.

    Returns ``None`` for windows under 1 s — the guard that keeps phoneme
    noise out of the cluster space.
    """
    if audio is None or len(audio) < MIN_EMBED_SAMPLES:
        return None
    try:
        import torch

        signal = torch.from_numpy(audio).unsqueeze(0)
        vec = _get_encoder().encode_batch(signal).flatten().cpu().numpy()
        norm = np.linalg.norm(vec)
        if norm == 0:
            return None
        return (vec / norm).astype(np.float32)
    except Exception as e:
        log.warning("Speaker embedding failed: %s", e)
        return None


def _label(idx: int) -> str:
    return f"Speaker {idx + 1}"


class SpeakerSession:
    """Per-session speaker state: every utterance embedding plus the
    current cluster assignments. Not thread-safe; the websocket handler
    owns it and calls it from one task at a time."""

    def __init__(self) -> None:
        # chunk_id -> normalized embedding, in arrival order.
        self._embeddings: dict[int, np.ndarray] = {}
        # chunk_id -> cluster index. The assignment map IS the cluster
        # state — scoring works against members, not maintained centroids,
        # so a re-cluster rebuild is just rewriting this map.
        self._assignments: dict[int, int] = {}
        self._n_clusters = 0
        self._since_recluster = 0
        self._new_speaker_pending = False
        self._last_label: str | None = None
        # User-provided participant count (the industry-standard knob:
        # speakers_expected). None = estimate the count from the data.
        self._expected: int | None = None

    def set_expected(self, count: int | None) -> None:
        """Set how many speakers the meeting actually has. The user knows
        this in a Zoom call; with it, re-clustering cuts the tree at
        exactly that count instead of estimating it, and assignment never
        creates speakers beyond it. None or 0 returns to auto."""
        if count:
            self._expected = max(1, min(int(count), MAX_SPEAKERS))
        else:
            self._expected = None

    def fallback_label(self) -> str:
        """Best label for an utterance with no usable embedding: speaker
        continuity (the previous utterance's label) beats a hardcoded
        'Speaker 1' once the meeting is underway."""
        return self._last_label or _label(0)

    def _cluster_scores(self, emb: np.ndarray) -> np.ndarray:
        """Score each cluster as the MEDIAN of its top-3 member
        similarities. Nearest-member alone chains through outliers; a
        centroid washes out a speaker's natural variance; the median of
        the three closest members tolerates one contaminating utterance
        while still rewarding genuinely close clusters."""
        scores = np.full(self._n_clusters, -1.0)
        sims_by_cluster: dict[int, list[float]] = {}
        for cid, idx in self._assignments.items():
            sims_by_cluster.setdefault(idx, []).append(float(self._embeddings[cid] @ emb))
        for idx, sims in sims_by_cluster.items():
            top = sorted(sims, reverse=True)[:3]
            scores[idx] = float(np.median(top))
        return scores

    def assign(self, chunk_id: int, emb: np.ndarray, duration_sec: float | None = None) -> str:
        """Immediately label one utterance and fold it into the clusters.

        ``duration_sec`` feeds the quality gate: an unknown duration is
        treated as long enough (trusted callers / tests)."""
        if self._n_clusters == 0:
            idx = 0
            self._n_clusters = 1
        else:
            scores = self._cluster_scores(emb)
            best = int(np.argmax(scores))
            cap = min(self._expected or MAX_SPEAKERS, MAX_SPEAKERS)
            too_short = duration_sec is not None and duration_sec < MIN_NEW_SPEAKER_SEC
            if scores[best] >= NEW_SPEAKER_THRESHOLD or self._n_clusters >= cap or too_short:
                idx = best
            else:
                idx = self._n_clusters
                self._n_clusters += 1
                self._new_speaker_pending = True

        self._embeddings[chunk_id] = emb
        self._assignments[chunk_id] = idx
        self._since_recluster += 1
        self._last_label = _label(idx)
        return self._last_label

    def maybe_recluster(self) -> dict[int, str]:
        """Re-cluster all embeddings every RECLUSTER_EVERY finals, or
        sooner (RECLUSTER_AFTER_NEW) when a new speaker just appeared —
        the moment assignment mistakes are most likely.

        Returns ``{chunk_id: new_label}`` for utterances whose label
        changed (empty most of the time). Cluster indices are re-mapped
        onto the previous labeling by greatest overlap so stable speakers
        keep their numbers and only the corrected utterances move.
        """
        due = self._since_recluster >= RECLUSTER_EVERY or (
            self._new_speaker_pending and self._since_recluster >= RECLUSTER_AFTER_NEW
        )
        if not due or len(self._embeddings) < 3:
            return {}
        self._since_recluster = 0
        self._new_speaker_pending = False

        chunk_ids = list(self._embeddings.keys())
        matrix = np.stack([self._embeddings[c] for c in chunk_ids])
        groups = _agglomerative(
            matrix,
            distance_threshold=1.0 - MATCH_THRESHOLD,
            expected=self._expected,
        )

        # Map new group ids onto existing labels by overlap, largest first,
        # so an early mislabel can't steal a bigger speaker's number.
        overlap: dict[tuple[int, int], int] = {}
        for cid, g in zip(chunk_ids, groups, strict=False):
            old = self._assignments[cid]
            overlap[(g, old)] = overlap.get((g, old), 0) + 1
        mapping: dict[int, int] = {}
        used: set[int] = set()
        for (g, old), _n in sorted(overlap.items(), key=lambda kv: -kv[1]):
            if g in mapping or old in used:
                continue
            mapping[g] = old
            used.add(old)
        next_free = 0
        for g in sorted(set(groups)):
            if g not in mapping:
                while next_free in used:
                    next_free += 1
                mapping[g] = next_free
                used.add(next_free)

        # Rebuild the assignment map from scratch — this is the self-heal.
        self._n_clusters = max(mapping.values()) + 1
        changes: dict[int, str] = {}
        for cid, g in zip(chunk_ids, groups, strict=False):
            idx = mapping[g]
            if self._assignments[cid] != idx:
                changes[cid] = _label(idx)
            self._assignments[cid] = idx

        if self._embeddings:
            last_chunk = max(self._embeddings.keys())
            self._last_label = _label(self._assignments[last_chunk])
        return changes


def _agglomerative(
    matrix: np.ndarray,
    distance_threshold: float,
    expected: int | None = None,
) -> list[int]:
    """Average-linkage agglomerative clustering on normalized embeddings.

    With ``expected`` set (the user said how many people are in the
    meeting), the tree is simply cut at that many groups — except when
    the whole tree is already tight, which means fewer people have
    actually spoken so far and forcing the count would split someone.

    Otherwise the cluster count comes from the tree itself, not an
    absolute cutoff: within-speaker merges happen at low cosine distance
    and cross-speaker merges at high distance, so the largest gap between
    consecutive merge heights marks the natural boundary (for two people
    talking, the final cross-speaker merge sits far above everything else
    and the cut yields exactly 2). Absolute thresholds only guard the
    edges: a tree whose final merge is already tight is one speaker, and
    an indecisive tree (no gap above MIN_GAP) falls back to
    ``distance_threshold``. The group count is then capped at
    MAX_SPEAKERS by merging the smallest leftover groups into their
    nearest large neighbour.
    """
    n = len(matrix)
    if n == 1:
        return [0]
    from scipy.cluster.hierarchy import fcluster, linkage

    link = linkage(matrix, method="average", metric="cosine")
    heights = link[:, 2]

    if heights[-1] <= SINGLE_CLUSTER_DIST:
        return [0] * n

    if expected is not None:
        flat = fcluster(link, t=min(expected, n), criterion="maxclust")
        return _compact([int(g) - 1 for g in flat])

    best_gap, cut = 0.0, None
    for i in range(len(heights) - 1):
        n_groups = n - i - 1  # clusters left after cutting above merge i
        if n_groups < 2 or n_groups > MAX_SPEAKERS:
            continue
        gap = float(heights[i + 1] - heights[i])
        if gap > best_gap:
            best_gap, cut = gap, float(heights[i] + heights[i + 1]) / 2
    if cut is None or best_gap < MIN_GAP:
        cut = distance_threshold

    flat = fcluster(link, t=cut, criterion="distance")
    groups = [int(g) - 1 for g in flat]

    uniq = sorted(set(groups))
    if len(uniq) > MAX_SPEAKERS:
        sizes = {g: groups.count(g) for g in uniq}
        keep = sorted(uniq, key=lambda g: -sizes[g])[:MAX_SPEAKERS]
        keep_set = set(keep)
        centroids = {
            g: matrix[[i for i, x in enumerate(groups) if x == g]].mean(axis=0) for g in uniq
        }
        for i, g in enumerate(groups):
            if g not in keep_set:
                sims = {k: float(np.dot(centroids[g], centroids[k])) for k in keep}
                groups[i] = max(sims, key=sims.get)

    return _compact(groups)


def _compact(groups: list[int]) -> list[int]:
    """Renumber group ids to a dense 0..k-1 range."""
    remap = {g: i for i, g in enumerate(sorted(set(groups)))}
    return [remap[g] for g in groups]


# ── Per-session registry (RAM only) ─────────────────────────────────────
# Keyed by session_id so a reconnect or second tab on the same conversation
# keeps its speaker labels. No disk persistence: labels are session-scoped
# by design, and a server restart starting fresh at "Speaker 1" is fine.

_sessions: dict[str, SpeakerSession] = {}
_sessions_lock = threading.Lock()


def get_session(session_id: str | None) -> SpeakerSession:
    """The shared SpeakerSession for this id (created on first use).
    No id -> a fresh ephemeral session that nobody else can see."""
    if not session_id:
        return SpeakerSession()
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if sess is None:
            sess = SpeakerSession()
            _sessions[session_id] = sess
        return sess


def drop_session(session_id: str | None) -> None:
    """Forget a session's speaker state (explicit stop / session delete)."""
    if not session_id:
        return
    with _sessions_lock:
        _sessions.pop(session_id, None)
