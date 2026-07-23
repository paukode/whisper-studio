"""Monotonic chunk-id resume across WebSocket reconnects.

A finalized transcript chunk's id keys persisted speaker memory
(SpeakerSession._embeddings / _assignments) and is echoed back in later
``speaker_update`` corrections. That speaker state survives a reconnect
in diarization's RAM registry, so the id counter must too — otherwise a
reconnect for the same session restarts at 0 and reuses ids that already
belong to earlier chunks, overwriting their embeddings and mis-routing
corrections. These tests exercise the counter store directly (hermetic:
no real WebSocket, no ASR, no diarization model)."""

import pytest

from server.websocket import (
    _chunk_counters,
    _next_chunk_start,
    _record_chunk_counter,
    _reset_chunk_counter,
)


@pytest.fixture(autouse=True)
def _clean_counter_store():
    """The counter store is module-global; keep each test independent."""
    _chunk_counters.clear()
    yield
    _chunk_counters.clear()


def _drain(session_id, start, n):
    """Mimic emit_events: hand out ``n`` chunk ids from ``start`` and
    persist the next-unused id after each, exactly as the handler does.
    Returns the ids issued (for collision checks)."""
    counter = start
    issued = []
    for _ in range(n):
        chunk_id = counter
        counter += 1
        _record_chunk_counter(session_id, counter)
        issued.append(chunk_id)
    return issued


def test_new_session_starts_at_zero():
    assert _next_chunk_start("sess-new") == 0


def test_reconnect_resumes_after_emitted_chunks():
    # First connect: emit 5 chunks (ids 0..4).
    first = _drain("sess-a", _next_chunk_start("sess-a"), 5)
    assert first == [0, 1, 2, 3, 4]
    # Reconnect (mic dropped / tab refresh) resumes at 5, NOT 0.
    assert _next_chunk_start("sess-a") == 5


def test_reconnect_ids_never_collide_with_earlier_chunks():
    # The corruption scenario: without resume, the reconnect would reissue
    # 0,1,2 and clobber the first connection's speaker memory.
    first = _drain("sess-b", _next_chunk_start("sess-b"), 3)
    second = _drain("sess-b", _next_chunk_start("sess-b"), 3)
    assert first == [0, 1, 2]
    assert second == [3, 4, 5]
    assert set(first).isdisjoint(second)


def test_sessions_are_independent():
    _drain("sess-x", _next_chunk_start("sess-x"), 7)
    # A different session id is untouched by sess-x's activity.
    assert _next_chunk_start("sess-y") == 0
    assert _next_chunk_start("sess-x") == 7


def test_reset_returns_to_zero():
    # Explicit stop drops speaker state and the counter together, so a
    # later session under the same id starts fresh.
    _drain("sess-c", _next_chunk_start("sess-c"), 4)
    assert _next_chunk_start("sess-c") == 4
    _reset_chunk_counter("sess-c")
    assert _next_chunk_start("sess-c") == 0


def test_no_session_id_is_ephemeral_and_never_persists():
    # An id-less (or dictation) connection always starts at 0 and its
    # record/reset calls are harmless no-ops that touch no shared state.
    assert _next_chunk_start(None) == 0
    _record_chunk_counter(None, 9)
    _reset_chunk_counter(None)
    assert _next_chunk_start(None) == 0
    assert _chunk_counters == {}
