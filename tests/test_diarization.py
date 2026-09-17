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
    is_filler,
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


# ── spurious-speaker gates ────────────────────────────────────────────────────
# Backchannels are the failure mode duration gates never caught: "mm-hmm"
# thrown over somebody else's sentence is loud, long enough, and
# acoustically a blend of two voices, so it lands far from every cluster
# and looks exactly like a new person.


def test_filler_text_is_recognised():
    assert is_filler("uh")
    assert is_filler("Mm-hmm.")
    assert is_filler("oh, yeah")
    assert is_filler("  ")
    assert is_filler("a")  # too short to carry identity
    assert not is_filler("that's the whole point")
    assert not is_filler(None)


def test_filler_utterance_never_creates_a_speaker():
    s = SpeakerSession()
    s.assign(0, basis(0), 5.0, "the quarterly numbers are in")
    # Acoustically nothing like speaker 1 — normally a brand-new cluster.
    assert s.assign(1, basis(1), 5.0, "mm-hmm") == "Speaker 1"
    assert s._n_clusters == 1


def test_real_words_still_create_a_speaker():
    s = SpeakerSession()
    s.assign(0, basis(0), 5.0, "the quarterly numbers are in")
    assert s.assign(1, basis(1), 5.0, "actually I disagree with that") == "Speaker 2"


def test_undersized_cluster_is_dissolved_on_recluster():
    s = SpeakerSession()
    # Two real speakers with plenty of speech...
    for i, chunk in enumerate((0, 1, 2, 3)):
        s.assign(chunk, mix(0.95, i % 2, 0.31, 2 + i % 2), 6.0, "a real sentence here")
    # ...plus a stray fragment that clustered on its own.
    s.assign(9, basis(5), 0.9, "some words")
    s._since_recluster = RECLUSTER_EVERY
    s.maybe_recluster()
    totals = {}
    for chunk, group in s._assignments.items():
        totals[group] = totals.get(group, 0.0) + s._durations.get(chunk, 0.0)
    assert all(total >= 3.0 for total in totals.values())


def test_dissolve_is_skipped_while_everyone_is_still_small():
    """Early in a meeting nobody has cleared the bar yet — leave it alone."""
    s = SpeakerSession()
    s.assign(0, basis(0), 1.0, "first words")
    s.assign(1, basis(1), 1.0, "different voice entirely")
    s.assign(2, basis(2), 1.0, "and a third one")
    s._since_recluster = RECLUSTER_EVERY
    s.maybe_recluster()
    assert s._n_clusters == 3


# ── per-encoder threshold calibration ────────────────────────────────────────
# Cosine similarity means something different in each embedding space:
# ReDimNet2 puts the same speaker at 0.83-0.90 and different speakers at
# 0.02-0.52, where ECAPA puts them at 0.25-0.55 and 0.0-0.2. Running one
# encoder's numbers against the other labels the whole meeting "Speaker 1".


def test_thresholds_follow_the_loaded_encoder(monkeypatch):
    from server.diarization import embedder
    from server.diarization import speakers as sp

    monkeypatch.setattr(embedder, "active_name", lambda: embedder.NAME_REDIMNET)
    assert sp._thresholds() == sp.THRESHOLDS[embedder.NAME_REDIMNET]
    monkeypatch.setattr(embedder, "active_name", lambda: embedder.NAME_ECAPA)
    assert sp._thresholds() == sp.THRESHOLDS[embedder.NAME_ECAPA]
    # Nothing loaded: keep the historical calibration.
    monkeypatch.setattr(embedder, "active_name", lambda: None)
    assert sp._thresholds() == sp.THRESHOLDS[embedder.NAME_ECAPA]


def test_a_different_voice_creates_a_speaker_under_redimnet(monkeypatch):
    """0.30 similarity is a weak-but-plausible match for ECAPA and a clear
    stranger for ReDimNet2, and each encoder must be judged by its own bar."""
    from server.diarization import embedder

    monkeypatch.setattr(embedder, "active_name", lambda: embedder.NAME_REDIMNET)
    s = SpeakerSession()
    s.assign(0, basis(0), 5.0, "a real sentence")
    assert s.assign(1, mix(0.30, 0, 0.954, 1), 5.0, "another real sentence") == "Speaker 2"

    monkeypatch.setattr(embedder, "active_name", lambda: embedder.NAME_ECAPA)
    t = SpeakerSession()
    t.assign(0, basis(0), 5.0, "a real sentence")
    assert t.assign(1, mix(0.30, 0, 0.954, 1), 5.0, "another real sentence") == "Speaker 1"
