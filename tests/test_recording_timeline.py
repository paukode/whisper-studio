"""The recording timeline in the backend log (server/recording_timeline.py).

A field report like "the first words showed, then vanished and never reached
the transcript" can only be settled from the log: when audio first arrived,
how loud it was, when the engine was ready, when the first draft and final
went out, and, above all, every draft withdrawn without a final with its
words and the reason. The unit tests drive the timeline with a fake clock;
the end-to-end tests run the real /ws handler with a scripted backend.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.websocket as ws_mod
from server import diarization
from server.diarization import speakers
from server.recording_timeline import RecordingTimeline

_SECOND = b"\x00\x00" * 16000  # 1 s of digital silence


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _tone(seconds: float, amplitude: float = 0.1) -> bytes:
    t = np.arange(int(16000 * seconds)) / 16000
    return (np.sin(2 * np.pi * 440 * t) * amplitude * 32767).astype(np.int16).tobytes()


def _lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "whisper-studio"]


def _timeline(clock: _Clock, take: str = "take-a", dictation: bool = False) -> RecordingTimeline:
    return RecordingTimeline(
        take_id=take,
        session_id="sess-1234567890",
        backend="streaming",
        dictation=dictation,
        clock=clock,
    )


# ── unit: one line per fact, with the timing it needs ────────────────────


def test_a_withdrawn_draft_is_a_warning_with_its_words_and_reason(caplog):
    clock = _Clock()
    with caplog.at_level("INFO", logger="whisper-studio"):
        timeline = _timeline(clock)
        timeline.audio(_tone(1.0))
        clock.now += 1.0
        timeline.draft("Good morning")
        timeline.draft("Good morning everyone")  # the same draft growing
        clock.now += 1.0
        timeline.withdrawn(
            {
                "draft": "Good morning everyone",
                "reason": "too little speech for the VAD",
                "voiced_ms": 330,
                "length_ms": 1200,
                "at": "pause",
            }
        )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    line = warnings[0].getMessage()
    assert line.startswith("[rec take-a] DRAFT DISCARDED, not added to the transcript")
    assert '"Good morning everyone"' in line
    assert "reason: too little speech for the VAD (330 ms voiced of 1200 ms, at pause)" in line
    # A growing draft is one draft: only its first text is logged.
    drafts = [m for m in _lines(caplog) if "draft shown" in m]
    assert len(drafts) == 1 and '"Good morning"' in drafts[0]


def test_the_lag_line_says_how_far_the_server_is_behind_live_audio(caplog):
    clock = _Clock()
    with caplog.at_level("INFO", logger="whisper-studio"):
        timeline = _timeline(clock)
        timeline.audio(_tone(0.32))
        # The server stalls for 10 s (a model paged back in) before the next
        # frame is read and the first final goes out.
        clock.now += 10.0
        timeline.audio(_tone(0.32))
        timeline.decoded(9.5)
        timeline.final("Good morning everyone.", 0, "Speaker 1", 9.6)
    final = next(m for m in _lines(caplog) if "final #0" in m)
    assert "0.6 s of audio in, 9.4 s behind live" in final
    assert "speaker label took 9.6 s" in final
    assert any("decoding one chunk took 9.5 s" in m for m in _lines(caplog))
    assert any("no audio read for 10.0 s" in m for m in _lines(caplog))


def test_first_audio_and_per_second_levels_show_a_silent_start(caplog):
    clock = _Clock()
    with caplog.at_level("INFO", logger="whisper-studio"):
        timeline = _timeline(clock)
        # The client buffered 2 s before the socket opened: one big frame.
        timeline.audio(_SECOND + _SECOND)
        timeline.audio(_tone(1.0))
        timeline.closed(by_stop=True)
    lines = _lines(caplog)
    assert any(
        "first audio at +0.00s: 2.00 s of audio in the first frame (the client buffered it)" in m
        for m in lines
    )
    levels = next(m for m in lines if "level of the first" in m)
    assert levels.endswith("silent silent -23")
    assert any("closed after" in m and "(stop)" in m and "3.0 s of audio" in m for m in lines)


def test_levels_are_logged_once_after_ten_seconds(caplog):
    clock = _Clock()
    with caplog.at_level("INFO", logger="whisper-studio"):
        timeline = _timeline(clock)
        for _ in range(12):
            timeline.audio(_tone(1.0))
        timeline.closed(by_stop=False)
    levels = [m for m in _lines(caplog) if "level of the first" in m]
    assert len(levels) == 1
    assert "first 10 s" in levels[0]
    assert any("dropped without stop" in m for m in _lines(caplog))


def test_client_marks_log_only_known_fields_on_one_line(caplog):
    clock = _Clock()
    with caplog.at_level("INFO", logger="whisper-studio"):
        timeline = _timeline(clock)
        timeline.client_mark(
            {
                "type": "client_mark",
                "mark": "mic_ready\nforged line",
                "ms": 2412.6,
                "device": "MacBook Pro Microphone",
                "secret": "not logged",
            }
        )
    line = next(m for m in _lines(caplog) if "client:" in m)
    assert "\n" not in line
    assert (
        'client: mic_ready forged line at 2412 ms after Record (device="MacBook Pro Microphone")'
        in line
    )
    assert "not logged" not in line


def test_after_the_first_minute_only_lost_drafts_are_logged(caplog):
    clock = _Clock()
    with caplog.at_level("INFO", logger="whisper-studio"):
        timeline = _timeline(clock)
        timeline.audio(_tone(1.0))
        for i in range(3):  # the first finals are always logged
            timeline.final(f"early {i}", i, "Speaker 1", 0.1)
        clock.now += 61.0
        timeline.draft("late draft")
        timeline.final("late final", 3, "Speaker 1", 0.1)
        timeline.draft("lost late")
        timeline.withdrawn(
            {"draft": "lost late", "reason": "no words in the final decode", "decoded": ["."]}
        )
    lines = _lines(caplog)
    assert not any("late draft" in m or "late final" in m for m in lines)
    assert any('"lost late"' in m and 'final decode gave "."' in m for m in lines)


def test_a_reconnect_is_numbered_within_its_take(caplog):
    clock = _Clock()
    with caplog.at_level("INFO", logger="whisper-studio"):
        _timeline(clock, take="take-reconnect")
        _timeline(clock, take="take-reconnect")
        _timeline(clock, dictation=True)
    lines = [m for m in _lines(caplog) if "connected:" in m]
    assert "connection 1 of this take" in lines[0] and "reconnect" not in lines[0]
    assert "connection 2 of this take (a reconnect)" in lines[1]
    assert lines[2].startswith("[dictation] connected")


# ── end to end through the real /ws handler ──────────────────────────────


class _ScriptedSession:
    def __init__(self, script: list[list[dict]]):
        self._script = script

    def process(self, raw_pcm: bytes) -> list[dict]:
        return self._script.pop(0) if self._script else []

    def finish(self) -> list[dict]:
        return []

    def close(self) -> None:
        pass


class _ScriptedBackend:
    def __init__(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="timeline-asr")
        self.script: list[list[dict]] = []

    def is_loaded(self) -> bool:
        return True

    def load(self) -> None:
        pass

    def create_session(self) -> _ScriptedSession:
        session, self.script = _ScriptedSession(self.script), []
        return session


@pytest.fixture
def ws_client(monkeypatch):
    backend = _ScriptedBackend()
    conf = {
        "transcription_backend": "streaming",
        "translate_mode": "off",
        "speaker_split_turns": False,
    }
    monkeypatch.setattr(ws_mod, "get_backend", lambda name: backend)
    monkeypatch.setattr(ws_mod, "config_get", lambda key, default=None: conf.get(key, default))
    monkeypatch.setattr(diarization, "embed", lambda audio: None)
    ws_mod._chunk_counters.clear()
    ws_mod._carryover.clear()
    app = FastAPI()
    app.include_router(ws_mod.router)
    with TestClient(app, base_url="http://localhost") as client:
        yield client, backend
    ws_mod._chunk_counters.clear()
    ws_mod._carryover.clear()
    for sid in [k for k in speakers._sessions if k.startswith("timeline-test-")]:
        diarization.drop_session(sid)
    backend.executor.shutdown(wait=True)


def _until_ended(ws) -> list[dict]:
    ws.send_json({"type": "stop"})
    frames = []
    while (frame := ws.receive_json()) != {"type": "session_ended"}:
        frames.append(frame)
    return frames


def test_a_recording_logs_its_start_and_the_draft_it_lost(ws_client, caplog):
    """The scenario from the field: the first words show as a draft, the
    utterance closes without a final, the draft leaves the screen. The log
    names the words and the reason, between the start-up lines."""
    client, backend = ws_client
    why = {
        "draft": "Good morning",
        "reason": "too little speech for the VAD",
        "voiced_ms": 330,
        "length_ms": 900,
        "at": "pause",
    }
    backend.script = [
        [{"kind": "interim", "text": "Good morning"}],
        [{"kind": "interim", "text": "", "withdrawn": why}],
        [
            {
                "kind": "final",
                "text": "Thanks for joining.",
                "audio": np.zeros(16000, dtype=np.float32),
            }
        ],
    ]
    with caplog.at_level("INFO", logger="whisper-studio"):
        with client.websocket_connect(
            "/ws?session_id=timeline-test-1&backend=streaming&take=take-e2e-1"
        ) as ws:
            ws.send_json(
                {
                    "type": "client_mark",
                    "mark": "mic_ready",
                    "ms": 850,
                    "device": "MacBook Pro Microphone",
                }
            )
            for _ in range(3):
                ws.send_bytes(_tone(0.32))
            frames = _until_ended(ws)
        # The handler logs its close after the socket ends.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any("closed after" in m for m in _lines(caplog)):
            time.sleep(0.01)

    # The client still sees exactly what it saw before: no new wire fields.
    assert frames[:2] == [
        {"type": "interim", "text": "Good morning"},
        {"type": "interim", "text": ""},
    ]
    lines = [m for m in _lines(caplog) if m.startswith("[rec take-e2e")]
    order = [
        "connected: backend=parakeet",
        "client: mic_ready at 850 ms after Record",
        "first audio at",
        "ASR session ready",
        "draft shown at",
        "DRAFT DISCARDED, not added to the transcript",
        "final #0",
        "closed after",
    ]
    positions = [next(i for i, m in enumerate(lines) if key in m) for key in order]
    assert positions == sorted(positions), lines
    lost = next(m for m in lines if "DRAFT DISCARDED" in m)
    assert '"Good morning"' in lost and "reason: too little speech for the VAD" in lost
    closed = next(m for m in lines if "closed after" in m)
    assert "(stop)" in closed and "1 drafts discarded" in closed and "1 finals" in closed
