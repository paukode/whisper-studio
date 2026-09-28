"""The transcription websocket (server/websocket.py) end to end.

A scripted fake backend stands in for the ASR engine and speaker embedding is
stubbed out, so no model loads. Everything else is the real handler: the
emit path, chunk ids, translation scheduling, and connection teardown.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.websocket as ws_mod
from server import diarization
from server.asr import canary_backend
from server.diarization import speakers

_PCM = b"\x00\x00" * 1600  # 0.1 s of PCM16 silence; the fake backend ignores it


def _final(text: str, language: str | None = "pl") -> dict:
    return {
        "kind": "final",
        "text": text,
        "audio": np.zeros(16000, dtype=np.float32),
        "language": language,
    }


class _FakeSession:
    """Replays one scripted event list per process() call; finish() returns
    whatever the backend's ``tail`` held when the session was built."""

    def __init__(self, script: list[list[dict]], tail: list[dict]):
        self._script = script
        self._tail = tail
        self.closed = False

    def process(self, raw_pcm: bytes) -> list[dict]:
        return self._script.pop(0) if self._script else []

    def finish(self) -> list[dict]:
        out, self._tail = self._tail, []
        return out

    def close(self) -> None:
        self.closed = True


class _FakeBackend:
    def __init__(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fake-asr")
        self.script: list[list[dict]] = []
        self.tail: list[dict] = []
        self.sessions: list[_FakeSession] = []

    def is_loaded(self) -> bool:
        return True

    def load(self) -> None:
        pass

    def create_session(self) -> _FakeSession:
        session = _FakeSession(self.script, self.tail)
        self.script, self.tail = [], []
        self.sessions.append(session)
        return session


@pytest.fixture
def harness(monkeypatch):
    backend = _FakeBackend()
    conf = {
        "transcription_backend": "whisper",
        "translate_mode": "off",
        "translate_target": "en",
        "speaker_split_turns": False,
    }
    monkeypatch.setattr(ws_mod, "get_backend", lambda name: backend)
    monkeypatch.setattr(ws_mod, "config_get", lambda key, default=None: conf.get(key, default))
    # No speaker encoder: every final takes the continuity label.
    monkeypatch.setattr(diarization, "embed", lambda audio: None)
    # Canary never loads: a translation decode returns a fixed line.
    monkeypatch.setattr(canary_backend, "_generate", lambda a, source_lang, target_lang: "hello")
    monkeypatch.setattr(canary_backend, "_pending_translations", 0)
    ws_mod._chunk_counters.clear()
    ws_mod._carryover.clear()
    app = FastAPI()
    app.include_router(ws_mod.router)
    # One portal for every connection, so they share an event loop as they
    # do under uvicorn.
    with TestClient(app, base_url="http://localhost") as client:
        yield client, backend, conf
    ws_mod._chunk_counters.clear()
    ws_mod._carryover.clear()
    for sid in [k for k in speakers._sessions if k.startswith("ws-test-")]:
        diarization.drop_session(sid)
    backend.executor.shutdown(wait=True)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _frames_until_pong(ws) -> list[dict]:
    """Every frame the handler sends before answering a ping. The handler
    works through messages in order, so this is all it produced for what was
    sent before the ping, and a missing frame fails the test instead of
    blocking it forever."""
    ws.send_json({"type": "ping"})
    frames = []
    while (frame := ws.receive_json()) != {"type": "pong"}:
        frames.append(frame)
    return frames


def _frames_until_ended(ws) -> list[dict]:
    """Stop the take and collect every frame up to ``session_ended``. Stop
    drains in-flight translations first, so they are all in here."""
    ws.send_json({"type": "stop"})
    frames = []
    while (frame := ws.receive_json()) != {"type": "session_ended"}:
        frames.append(frame)
    return frames


# ── dictation never translates ───────────────────────────────────────────


def test_dictation_never_schedules_a_translation(harness, monkeypatch):
    """The chat mic renders no translation line, so a dictation connection
    must not queue a Canary decode even when Translate is armed, and even if
    a client sends set_translate. A recorder connection under the same
    config does translate: the difference is the dictation flag alone."""
    client, backend, conf = harness
    conf["translate_mode"] = "canary"
    queued: list[int] = []
    real_queued = canary_backend.note_translation_queued
    monkeypatch.setattr(
        canary_backend,
        "note_translation_queued",
        lambda: (queued.append(1), real_queued())[1],
    )

    backend.script = [[_final("dzień dobry")]]
    with client.websocket_connect("/ws?dictation=1") as ws:
        ws.send_json({"type": "set_translate", "mode": "canary", "target": "en"})
        ws.send_bytes(_PCM)
        frames = _frames_until_ended(ws)
    assert [(f["type"], f["text"]) for f in frames] == [("transcript", "dzień dobry")]
    assert "translating" not in frames[0]
    assert queued == []

    backend.script = [[_final("dzień dobry")]]
    with client.websocket_connect("/ws?session_id=ws-test-rec") as ws:
        ws.send_bytes(_PCM)
        frame, translation = _frames_until_ended(ws)
    assert frame["translating"] is True
    assert translation == {
        "type": "translation",
        "chunk_id": frame["chunk_id"],
        "text": "hello",
        "target": "en",
    }
    assert queued == [1]


# ── translation accounting across a dropped socket ───────────────────────


def test_translation_cancelled_in_the_queue_releases_the_draft_gate(harness):
    """A socket that closes without a stop cancels its queued translations.
    A job cancelled before it ran must still be counted out of Canary's draft
    scheduler, or every Canary session in the process stops showing live
    drafts until restart."""
    client, backend, conf = harness
    conf["translate_mode"] = "canary"
    release = threading.Event()
    # Occupy Canary's single decode thread so the translation stays queued.
    blocker = canary_backend.executor.submit(release.wait, 10)
    try:
        backend.script = [[_final("dzień dobry")]]
        with client.websocket_connect("/ws?session_id=ws-test-drop") as ws:
            ws.send_bytes(_PCM)
            (frame,) = _frames_until_pong(ws)
            assert frame["translating"] is True
            assert _wait_until(canary_backend._translations_waiting)
        # Disconnected without a stop: the queued job was cancelled.
    finally:
        release.set()
        blocker.result(timeout=5)
    canary_backend.executor.submit(lambda: None).result(timeout=5)
    assert not canary_backend._translations_waiting()


def test_translation_that_runs_is_counted_out_once(harness):
    """The ordinary path: the job runs, is counted out on entry, and the
    count returns to zero rather than drifting either way."""
    client, backend, conf = harness
    conf["translate_mode"] = "canary"
    backend.script = [[_final("dzień dobry")], [_final("do widzenia")]]
    with client.websocket_connect("/ws?session_id=ws-test-run") as ws:
        ws.send_bytes(_PCM)
        ws.send_bytes(_PCM)
        frames = _frames_until_ended(ws)
    assert sorted(f["type"] for f in frames) == ["transcript"] * 2 + ["translation"] * 2
    assert canary_backend._pending_translations == 0


# ── draft withdrawal reaches the client ──────────────────────────────────


def test_empty_interim_is_relayed_to_withdraw_the_draft(harness):
    """A backend withdraws a draft whose utterance produced no final with an
    empty interim. The client clears its draft on it, so the handler must
    pass it through instead of skipping it as empty text."""
    client, backend, _ = harness
    draft = "No, no, no, I didn't say that"
    backend.script = [
        [{"kind": "interim", "text": draft}],
        [{"kind": "interim", "text": ""}],
    ]
    with client.websocket_connect("/ws?session_id=ws-test-draft") as ws:
        ws.send_bytes(_PCM)
        assert _frames_until_pong(ws) == [{"type": "interim", "text": draft}]
        ws.send_bytes(_PCM)
        assert _frames_until_pong(ws) == [{"type": "interim", "text": ""}]
        assert _frames_until_ended(ws) == []


# ── the in-flight utterance survives a dropped socket ────────────────────


def _recorder(session_id: str, take: str) -> str:
    return f"/ws?session_id={session_id}&take={take}"


def test_dropped_connection_hands_its_in_flight_utterance_to_the_reconnect(harness):
    """A socket that drops without a stop still holds the utterance in
    progress. It is flushed and relayed by the next connection of the same
    take, before that connection's own audio and with the next chunk id,
    instead of being thrown away."""
    client, backend, _ = harness
    backend.tail = [_final("to zdanie prawie przepadło")]
    with client.websocket_connect(_recorder("ws-test-carry", "take-1")) as ws:
        ws.send_bytes(_PCM)
        assert _frames_until_pong(ws) == []
    # Dropped: no stop was sent.
    dropped = backend.sessions[0]

    backend.script = [[_final("mówię dalej")]]
    with client.websocket_connect(_recorder("ws-test-carry", "take-1")) as ws:
        ws.send_bytes(_PCM)
        frames = _frames_until_pong(ws)
        assert _frames_until_ended(ws) == []
    assert [(f["type"], f["text"]) for f in frames] == [
        ("transcript", "to zdanie prawie przepadło"),
        ("transcript", "mówię dalej"),
    ]
    assert frames[0]["chunk_id"] < frames[1]["chunk_id"]
    assert dropped.closed
    assert "ws-test-carry" not in ws_mod._carryover


def test_reconnect_waits_for_a_connection_that_has_not_let_go(harness):
    """The server can learn that a socket died later than the client does:
    the reconnect may already be sending audio while the old handler still
    holds its utterance. That older utterance must still come out first,
    not after sentences spoken later."""
    client, backend, _ = harness
    url = _recorder("ws-test-order", "take-1")
    backend.tail = [_final("stare zdanie")]
    old = client.websocket_connect(url)
    old_ws = old.__enter__()
    try:
        old_ws.send_bytes(_PCM)
        assert _frames_until_pong(old_ws) == []
        backend.script = [[_final("nowe zdanie")]]
        with client.websocket_connect(url) as ws:
            ws.send_bytes(_PCM)
            # The reconnect has reached its first audio and is waiting on
            # the old connection's place in the take.
            assert _wait_until(lambda: len(ws_mod._carryover["ws-test-order"].slots) == 1)
            old.__exit__(None, None, None)
            old = None
            frames = _frames_until_pong(ws)
            assert _frames_until_ended(ws) == []
    finally:
        if old is not None:
            old.__exit__(None, None, None)
    assert [f["text"] for f in frames] == ["stare zdanie", "nowe zdanie"]
    assert frames[0]["chunk_id"] < frames[1]["chunk_id"]
    assert ws_mod._carryover == {}


def test_a_new_take_never_gets_the_last_takes_utterance(harness):
    """When Stop never reached the server (the socket was down), the client
    already kept that take's last draft. The next take for the session is
    not a reconnect: replaying the old take's sentence into it would show it
    twice, once in the wrong take."""
    client, backend, _ = harness
    backend.tail = [_final("zdanie z poprzedniego nagrania")]
    with client.websocket_connect(_recorder("ws-test-takes", "take-1")) as ws:
        ws.send_bytes(_PCM)
        assert _frames_until_pong(ws) == []
    # Dropped, and the user's Stop never reached the server.

    backend.script = [[_final("nowe nagranie")]]
    with client.websocket_connect(_recorder("ws-test-takes", "take-2")) as ws:
        ws.send_bytes(_PCM)
        frames = _frames_until_pong(ws)
        assert _frames_until_ended(ws) == []
    assert [f["text"] for f in frames] == ["nowe nagranie"]
    # The old take's decoder is still flushed and released.
    assert _wait_until(lambda: backend.sessions[0].closed)
    assert ws_mod._carryover == {}


def test_nothing_outlives_the_stop_of_its_take(harness, monkeypatch):
    """A stop ends its take. An old connection of that take that lets go
    only after the stop (its handler was stuck) leaves nothing behind for a
    later connection to replay."""
    client, backend, _ = harness
    monkeypatch.setattr(ws_mod, "_CARRYOVER_WAIT_S", 0.05)
    url = _recorder("ws-test-late", "take-1")
    backend.tail = [_final("spóźnione zdanie")]
    with client.websocket_connect(url) as old_ws:
        old_ws.send_bytes(_PCM)
        assert _frames_until_pong(old_ws) == []
        with client.websocket_connect(url) as ws:
            # Stop gives up on the old connection after the bounded wait.
            assert _frames_until_ended(ws) == []
    assert _wait_until(lambda: backend.sessions[0].closed)
    assert ws_mod._carryover == {}

    backend.script = [[_final("następne")]]
    with client.websocket_connect(url) as ws:
        ws.send_bytes(_PCM)
        assert [f["text"] for f in _frames_until_pong(ws)] == ["następne"]


def test_stop_and_dictation_park_nothing(harness):
    """Only a recorder connection that dropped leaves anything behind. A
    stop has already flushed its tail to the client, and dictation text
    belongs to a composer that is gone."""
    client, backend, _ = harness
    backend.tail = [_final("ostatnie zdanie")]
    with client.websocket_connect(_recorder("ws-test-stopped", "take-1")) as ws:
        ws.send_bytes(_PCM)
        frames = _frames_until_ended(ws)
    assert [f["text"] for f in frames] == ["ostatnie zdanie"]

    backend.tail = [_final("podyktowane")]
    with client.websocket_connect(_recorder("ws-test-dictated", "take-1") + "&dictation=1") as ws:
        ws.send_bytes(_PCM)
        assert _frames_until_pong(ws) == []
    assert _wait_until(lambda: all(s.closed for s in backend.sessions))
    assert ws_mod._carryover == {}


def test_a_take_nobody_resumes_is_forgotten(monkeypatch):
    """A dropped take whose client never came back holds its utterance's
    audio only for the TTL: the next connection to open anywhere clears it."""
    ws_mod._carryover.clear()
    slot = ws_mod._open_slot("ws-test-gone", "take-1")
    ws_mod._hand_over("ws-test-gone", slot, None)
    monkeypatch.setattr(ws_mod, "_CARRYOVER_TTL_S", -1.0)
    other = ws_mod._open_slot("ws-test-other", "take-1")
    assert "ws-test-gone" not in ws_mod._carryover
    ws_mod._end_take("ws-test-other", other)
    assert ws_mod._carryover == {}
