"""Speaker assignment and re-clustering, exercised with synthetic
embeddings so no model loads. Vectors are built from an orthogonal basis:
cos(mix(a, i, b, j), basis(i)) == a for unit vectors, giving exact
control over similarity to each cluster."""

import pytest

# Diarization clustering uses scipy, which CI does not install (it strips the
# heavy ML deps — torch/speechbrain — that pull it in transitively). Skip the
# whole module when scipy is absent rather than erroring.
pytest.importorskip("scipy")

import numpy as np

from server.diarization.speakers import (
    MAX_SPEAKERS,
    RECLUSTER_EVERY,
    SpeakerSession,
    drop_session,
    get_session,
)

DIM = 8


def basis(i: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1.0
    return v


def mix(a: float, i: int, b: float, j: int) -> np.ndarray:
    v = a * basis(i) + b * basis(j)
    return (v / np.linalg.norm(v)).astype(np.float32)


def test_first_utterance_is_speaker_one():
    s = SpeakerSession()
    assert s.assign(0, basis(0)) == "Speaker 1"


def test_similar_embedding_joins_existing_speaker():
    s = SpeakerSession()
    s.assign(0, basis(0))
    assert s.assign(1, mix(0.95, 0, 0.312, 1)) == "Speaker 1"


def test_dissimilar_embedding_creates_new_speaker():
    s = SpeakerSession()
    s.assign(0, basis(0))
    assert s.assign(1, basis(1)) == "Speaker 2"


def test_weak_match_joins_nearest_instead_of_spawning():
    # The historical failure mode: real-world same-speaker similarity often
    # lands well under 0.5, and treating that as "unknown" manufactured a
    # new speaker per utterance. 0.30 must join, not spawn.
    s = SpeakerSession()
    s.assign(0, basis(0))
    assert s.assign(1, mix(0.30, 0, 0.954, 1)) == "Speaker 1"


def test_speaker_cap_snaps_to_nearest():
    s = SpeakerSession()
    for i in range(MAX_SPEAKERS):
        # Orthogonal/negated directions: cos <= 0 between any two, so each
        # would create a new speaker if the cap didn't stop it.
        v = basis(i % DIM) if i < DIM else -basis(i % DIM)
        s.assign(i, v)
    labels = {s.assign(100 + i, basis(i % DIM)) for i in range(DIM)}
    assert all(int(lbl.split()[1]) <= MAX_SPEAKERS for lbl in labels)


def test_fallback_label_uses_continuity():
    s = SpeakerSession()
    assert s.fallback_label() == "Speaker 1"
    s.assign(0, basis(0))
    s.assign(1, basis(1))
    assert s.fallback_label() == "Speaker 2"


def test_outlier_member_does_not_chain_a_new_speaker_in():
    # Cluster A holds five clean utterances plus one contaminant that is
    # very close to speaker B. Median-of-top-3 scoring must not let B's
    # first real utterance chain into A through that single outlier.
    s = SpeakerSession()
    for i in range(5):
        s.assign(i, basis(0))
    s.assign(5, mix(0.30, 0, 0.954, 1))  # contaminant, provisionally in A
    assert s.assign(6, basis(1)) == "Speaker 2"


def test_recluster_corrects_provisional_join():
    s = SpeakerSession()
    for i in range(5):
        s.assign(i, basis(0))
    # Really speaker B, but lands in A's weak-match band -> joins A.
    v_b = mix(0.30, 0, 0.954, 1)
    assert s.assign(5, v_b) == "Speaker 1"
    changes = {}
    for i in range(6, 6 + RECLUSTER_EVERY):
        s.assign(i, basis(1))
        changes.update(s.maybe_recluster())
    # The provisional chunk was retro-corrected to B...
    assert changes.get(5) == "Speaker 2"
    # ...and the established speakers kept their numbers.
    assert all(cid not in changes for cid in range(5))


def test_gap_cut_survives_loose_within_speaker_similarity():
    # Two speakers whose own utterances only agree at cosine 0.35 (distance
    # 0.65 — above any sane absolute cutoff). The merge-gap heuristic must
    # still find exactly two clusters instead of four.
    s = SpeakerSession()
    s.assign(0, basis(0))
    s.assign(1, mix(0.35, 0, 0.937, 3))
    s.assign(2, basis(1))
    s.assign(3, mix(0.35, 1, 0.937, 4))
    s._since_recluster = RECLUSTER_EVERY
    assert s.maybe_recluster() == {}
    assert s._n_clusters == 2


def test_solo_speaker_stays_one_cluster():
    s = SpeakerSession()
    s.assign(0, basis(0))
    s.assign(1, mix(0.80, 0, 0.60, 3))
    s.assign(2, mix(0.80, 0, 0.60, 4))
    s.assign(3, basis(0))
    s._since_recluster = RECLUSTER_EVERY
    assert s.maybe_recluster() == {}
    assert s._n_clusters == 1


def test_recluster_noop_when_assignments_already_correct():
    s = SpeakerSession()
    for i in range(RECLUSTER_EVERY + 2):
        s.assign(i, basis(i % 2))
    assert s.maybe_recluster() == {}


def test_short_utterance_never_creates_a_speaker():
    # A 1.2 s fragment (cough, cross-talk) that matches nobody must join
    # the nearest cluster instead of defining a new identity. The same
    # embedding at proper length is allowed to create one.
    s = SpeakerSession()
    s.assign(0, basis(0), duration_sec=5.0)
    assert s.assign(1, basis(1), duration_sec=1.2) == "Speaker 1"
    assert s.assign(2, basis(2), duration_sec=4.0) == "Speaker 2"


def test_expected_speakers_caps_online_creation():
    s = SpeakerSession()
    s.set_expected(2)
    s.assign(0, basis(0))
    s.assign(1, basis(1))
    # A third orthogonal voice cannot exceed the user's stated count.
    assert s.assign(2, basis(2)) in ("Speaker 1", "Speaker 2")


def test_expected_speakers_forces_recluster_count():
    # Four embeddings whose within-speaker similarity (0.35) is so loose
    # that even the gap heuristic could be forgiven for splitting them;
    # the user saying "2 speakers" must yield exactly 2.
    s = SpeakerSession()
    s.set_expected(2)
    s.assign(0, basis(0))
    s.assign(1, mix(0.35, 0, 0.937, 3))
    s.assign(2, basis(1))
    s.assign(3, mix(0.35, 1, 0.937, 4))
    s._since_recluster = RECLUSTER_EVERY
    s.maybe_recluster()
    assert s._n_clusters == 2


def test_expected_speakers_does_not_split_a_solo_speaker():
    # "2 speakers" set, but only one person has spoken so far — the
    # single-cluster guard must win over the forced count.
    s = SpeakerSession()
    s.set_expected(2)
    s.assign(0, basis(0))
    s.assign(1, mix(0.80, 0, 0.60, 3))
    s.assign(2, mix(0.80, 0, 0.60, 4))
    s._since_recluster = RECLUSTER_EVERY
    assert s.maybe_recluster() == {}
    assert s._n_clusters == 1


def test_set_expected_clamps_and_resets():
    s = SpeakerSession()
    s.set_expected(99)
    assert s._expected == MAX_SPEAKERS
    s.set_expected(0)
    assert s._expected is None
    s.set_expected(None)
    assert s._expected is None


def test_registry_keyed_and_droppable():
    a = get_session("sess-x")
    assert get_session("sess-x") is a
    drop_session("sess-x")
    assert get_session("sess-x") is not a
    assert get_session(None) is not get_session(None)
    drop_session("sess-x")
    drop_session(None)  # no-ops must not raise
