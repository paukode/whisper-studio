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
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from server.diarization import embedder, voiceprints

log = logging.getLogger("whisper-studio")

# Speaker encoders are trained on ~3 s utterances. Below ~1 s the
# embedding is dominated by phoneme content, not speaker identity. The
# upstream VAD buffer makes most utterances comfortably exceed this bar.
MIN_EMBED_SAMPLES = embedder.MIN_EMBED_SAMPLES
# Threshold calibration. These are PER ENCODER: cosine similarity means
# something different in each embedding space, and reusing one encoder's
# numbers for another is how every utterance ends up as "Speaker 1".
#
# ECAPA cosine similarities on short (1-8 s) real-world utterances run far
# lower than the folklore 0.6+: the same speaker across two utterances
# typically lands 0.25-0.55 (worse with room reverb, played-back media, or
# music underneath), while different speakers land roughly 0.0-0.2. The
# original 0.60/0.45 pair was tuned against close-mic audio and
# over-segmented badly in practice (11 "speakers" from a 2-person clip).
#
# ReDimNet2 separates far more sharply, measured on real speech: the same
# speaker across different sentences lands 0.83-0.90, different speakers
# 0.02-0.52 (the top of that range being a deliberately similar-sounding
# pair). Its thresholds sit in the gap between those bands.
#
#   match     fallback re-clustering cut (similarity), used only when the
#             merge-gap heuristic in _agglomerative finds no clear count
#   new       below this the embedding is nobody we know, so create a
#             speaker; at or above it join the best cluster (provisionally
#             when weak) and let re-clustering correct any mistake
#   single    if even the final merge is tighter than this cosine
#             DISTANCE, everybody in the recording is one speaker
#
# Lower `new` if speakers get merged, raise it if one speaker gets split.
THRESHOLDS = {
    embedder.NAME_ECAPA: {"match": 0.40, "new": 0.15, "single": 0.60},
    embedder.NAME_REDIMNET: {"match": 0.45, "new": 0.45, "single": 0.45},
}
# The historical (ECAPA) calibration, kept as the module-level names so
# callers and tests that never load an encoder behave exactly as before.
MATCH_THRESHOLD = THRESHOLDS[embedder.NAME_ECAPA]["match"]
NEW_SPEAKER_THRESHOLD = THRESHOLDS[embedder.NAME_ECAPA]["new"]

# Utterances shorter than this never CREATE a new speaker — short
# windows give unreliable embeddings (a cough or cross-talk fragment
# must not define a new identity). They still get labels (nearest
# cluster) and re-clustering can still move them later. The same idea as
# diart's rho_update quality gate, applied to creation.
MIN_NEW_SPEAKER_SEC = 2.0
# Real meetings rarely exceed ~10 speakers; at the cap we snap to the
# nearest cluster instead of creating yet another spurious speaker.
MAX_SPEAKERS = 12
# Backchannels are the other half of the spurious-speaker problem, and
# duration gates alone never caught them: "mm-hmm" thrown over somebody
# else's sentence is loud, long enough, and acoustically a blend of two
# voices, so it lands far from every cluster and looks exactly like a new
# person. A filler-only utterance therefore never CREATES a speaker; it
# still gets the nearest label, and re-clustering can still move it.
FILLERS = {
    "a",
    "ah",
    "aha",
    "eh",
    "er",
    "erm",
    "h",
    "hm",
    "hmm",
    "huh",
    "m",
    "mhm",
    "mm",
    "mmhmm",
    "mmm",
    "oh",
    "ok",
    "okay",
    "right",
    "sure",
    "uh",
    "uhhuh",
    "uhm",
    "um",
    "wow",
    "yeah",
    "yep",
    "yes",
    "yup",
}
# ...and so does anything this short: one or two characters of transcript
# carry no identity regardless of which word they are.
MIN_NEW_SPEAKER_CHARS = 3
# A cluster holding less than this much total speech across the whole
# session is not a person, it is an artefact. Re-clustering folds it into
# its nearest neighbour instead of leaving it in the speaker list.
MIN_SPEAKER_TOTAL_SEC = 3.0
# Re-cluster after this many new finalized utterances...
RECLUSTER_EVERY = 10
# ...or this many when a brand-new speaker was just created — that is
# exactly when assignment mistakes happen, so don't sit on them.
RECLUSTER_AFTER_NEW = 3
# Tree cutting (cosine distance): if even the final merge is below this,
# everything is one speaker; and a merge-distance gap must be at least
# MIN_GAP to be trusted as the cluster-count boundary.
SINGLE_CLUSTER_DIST = THRESHOLDS[embedder.NAME_ECAPA]["single"]
MIN_GAP = 0.12

# Embedding (torch) work runs here, off the event loop and off the
# backends' decode threads.
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="diarize")


def _thresholds() -> dict:
    """Calibration for the encoder that is actually loaded.

    Falls back to the ECAPA numbers when nothing has been loaded yet:
    there are no embeddings to judge in that state anyway, and it keeps
    the historical behaviour for callers that inject their own vectors.
    """
    name = embedder.active_name() or embedder.NAME_ECAPA
    return THRESHOLDS.get(name, THRESHOLDS[embedder.NAME_ECAPA])


def preload() -> None:
    """Eagerly load the encoder (startup warmup). Blocks until loaded."""
    executor.submit(embedder.preload).result()


def embed(audio: np.ndarray) -> np.ndarray | None:
    """Float32 mono 16 kHz audio -> L2-normalized speaker embedding.

    Returns ``None`` for windows under 1 s — the guard that keeps phoneme
    noise out of the cluster space.
    """
    return embedder.encode(audio)


def is_filler(text: str | None) -> bool:
    """Is this utterance nothing but backchannel noise ("uh", "mm-hmm", "oh")?

    Used only to deny SPEAKER CREATION, never to deny a label, so a real
    one-word answer still shows up in the transcript under somebody.
    """
    if text is None:
        return False
    stripped = text.strip()
    if not stripped:
        return True
    if len(re.sub(r"[^\w]", "", stripped)) < MIN_NEW_SPEAKER_CHARS:
        return True
    tokens = [re.sub(r"[^\w]", "", t.lower()) for t in stripped.split()]
    tokens = [t for t in tokens if t]
    return bool(tokens) and all(t in FILLERS for t in tokens)


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
        # chunk_id -> seconds of speech, for the total-speech gate that
        # dissolves clusters too small to be a real participant.
        self._durations: dict[int, float] = {}
        self._n_clusters = 0
        self._since_recluster = 0
        self._new_speaker_pending = False
        self._last_label: str | None = None
        # User-provided participant count (the industry-standard knob:
        # speakers_expected). None = estimate the count from the data.
        self._expected: int | None = None
        # cluster index -> name recovered from the voiceprint gallery, and
        # the ones the caller has not been told about yet.
        self._names: dict[int, str] = {}
        self._pending_names: dict[int, str] = {}

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

    def assign(
        self,
        chunk_id: int,
        emb: np.ndarray,
        duration_sec: float | None = None,
        text: str | None = None,
    ) -> str:
        """Immediately label one utterance and fold it into the clusters.

        ``duration_sec`` and ``text`` feed the creation gates: an unknown
        duration is treated as long enough (trusted callers / tests), and
        a filler-only ``text`` can join a cluster but never start one."""
        if self._n_clusters == 0:
            idx = 0
            self._n_clusters = 1
        else:
            scores = self._cluster_scores(emb)
            best = int(np.argmax(scores))
            cap = min(self._expected or MAX_SPEAKERS, MAX_SPEAKERS)
            too_short = duration_sec is not None and duration_sec < MIN_NEW_SPEAKER_SEC
            if (
                scores[best] >= _thresholds()["new"]
                or self._n_clusters >= cap
                or too_short
                or is_filler(text)
            ):
                idx = best
            else:
                idx = self._n_clusters
                self._n_clusters += 1
                self._new_speaker_pending = True

        self._embeddings[chunk_id] = emb
        self._assignments[chunk_id] = idx
        if duration_sec is not None:
            self._durations[chunk_id] = float(duration_sec)
        self._since_recluster += 1
        self._last_label = _label(idx)
        self._maybe_name(idx)
        return self._last_label

    # ── Voiceprint gallery ──────────────────────────────────────────────

    def _members(self, idx: int) -> list[np.ndarray]:
        return [self._embeddings[c] for c, g in self._assignments.items() if g == idx]

    def _centroid(self, idx: int) -> np.ndarray | None:
        members = self._members(idx)
        if not members:
            return None
        vec = np.stack(members).mean(axis=0)
        norm = float(np.linalg.norm(vec))
        return (vec / norm).astype(np.float32) if norm > 0.0 else None

    def _maybe_name(self, idx: int) -> None:
        """Ask the gallery who this cluster is, once it is worth asking.

        Deferred until the cluster holds two utterances: a name pinned
        from a single noisy embedding is the one mistake that would be
        visible for the rest of the meeting.
        """
        if idx in self._names or len(self._members(idx)) < 2:
            return
        centroid = self._centroid(idx)
        if centroid is None:
            return
        try:
            name = voiceprints.match(centroid)
        except Exception as e:  # a broken gallery must not break labelling
            log.warning("Voiceprint match failed: %s", e)
            return
        if name and name not in self._names.values():
            self._names[idx] = name
            self._pending_names[idx] = name

    def take_new_names(self) -> dict[str, str]:
        """Names discovered since the last call, as {label: name}."""
        found = {_label(idx): name for idx, name in self._pending_names.items()}
        self._pending_names.clear()
        return found

    def known_names(self) -> dict[str, str]:
        """Every {label: name} resolved so far (for a reconnecting client)."""
        return {_label(idx): name for idx, name in self._names.items()}

    def embeddings_for(self, label: str) -> list[np.ndarray]:
        """Every embedding behind a label like "Speaker 2" (enrollment input)."""
        for idx in range(self._n_clusters):
            if _label(idx) == label:
                return self._members(idx)
        return []

    def adopt_name(self, label: str, name: str) -> None:
        """Record a user-assigned name so the gallery is not re-queried for it."""
        for idx in range(self._n_clusters):
            if _label(idx) == label:
                self._names[idx] = name
                self._pending_names.pop(idx, None)
                return

    def _dissolve_small(
        self, chunk_ids: list[int], groups: list[int], matrix: np.ndarray
    ) -> list[int]:
        """Fold clusters holding too little speech into their nearest neighbour.

        A person who has spoken for two seconds across an hour is not a
        person, they are a handful of backchannels and cross-talk that
        clustered together. Only runs while at least one real cluster
        survives, so an early meeting (everybody still under the bar)
        is left alone.
        """
        totals: dict[int, float] = {}
        for cid, g in zip(chunk_ids, groups, strict=False):
            totals[g] = totals.get(g, 0.0) + self._durations.get(cid, 0.0)
        # No durations recorded at all (trusted callers / tests): nothing to judge.
        if not any(totals.values()):
            return groups
        big = [g for g, total in totals.items() if total >= MIN_SPEAKER_TOTAL_SEC]
        small = [g for g in totals if g not in big]
        if not big or not small:
            return groups
        centroids = {
            g: matrix[[i for i, x in enumerate(groups) if x == g]].mean(axis=0) for g in totals
        }
        moved = [
            max(big, key=lambda k: float(centroids[g] @ centroids[k])) if g in small else g
            for g in groups
        ]
        log.debug("Dissolved %d undersized speaker cluster(s)", len(small))
        return _compact(moved)

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
            distance_threshold=1.0 - _thresholds()["match"],
            single_cluster_dist=_thresholds()["single"],
            expected=self._expected,
        )
        groups = self._dissolve_small(chunk_ids, groups, matrix)

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
        for idx in range(self._n_clusters):
            self._maybe_name(idx)
        return changes


def _agglomerative(
    matrix: np.ndarray,
    distance_threshold: float,
    expected: int | None = None,
    single_cluster_dist: float = SINGLE_CLUSTER_DIST,
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

    if heights[-1] <= single_cluster_dist:
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
