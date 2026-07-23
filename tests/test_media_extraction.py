"""Audio/video attachment extraction.

Heavy ASR (mlx-whisper) and Apple Vision frame OCR are verified live, not here.
These tests cover the deterministic logic: media routing through /api/upload,
the diarization label mapping (embeddings mocked so it is fast and stable), and
the small helpers.
"""

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.attachments as A
import server.extract.media as media


def test_is_media_ext():
    assert media.is_media_ext(".mp3")
    assert media.is_media_ext(".wav")
    assert media.is_media_ext(".mp4")
    assert media.is_media_ext(".mov")
    assert not media.is_media_ext(".pdf")
    assert not media.is_media_ext(".png")
    assert not media.is_media_ext("")


def test_frame_interval_cadence():
    # Short clips sample every ~10 s (the baseline cadence).
    assert media._frame_interval(30) == 10.0
    assert media._frame_interval(120) == 10.0
    # At exactly 20 frames' worth of 10 s spacing (200 s) we're still at 10 s.
    assert media._frame_interval(200) == 10.0
    # Longer videos stretch the interval so the retained count never exceeds
    # _MAX_FRAMES: duration / _MAX_FRAMES.
    assert media._frame_interval(600) == 30.0
    assert media._frame_interval(1200) == 60.0
    # Unknown duration falls back to the baseline cadence.
    assert media._frame_interval(0) == float(media._FRAME_INTERVAL_SEC)


def test_frame_interval_never_exceeds_max_frames():
    # For any plausible duration, duration / interval must stay <= _MAX_FRAMES.
    for dur in (5, 30, 199, 200, 201, 600, 3600, 7200):
        interval = media._frame_interval(dur)
        assert dur / interval <= media._MAX_FRAMES + 1e-9


def test_fmt_ts():
    assert media._fmt_ts(0) == "00:00"
    assert media._fmt_ts(65) == "01:05"
    assert media._fmt_ts(599) == "09:59"


def test_diarize_labels_distinct_speakers(monkeypatch):
    pytest.importorskip("scipy")  # clustering uses scipy; CI strips the ML stack
    import server.diarization.speakers as sp

    # Orthogonal embeddings -> the real clusterer must split them into two
    # speakers; segment 3 matches segment 1, so it reuses Speaker 1.
    vecs = iter(
        [
            np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
            np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        ]
    )
    monkeypatch.setattr(sp, "embed", lambda clip: next(vecs))
    audio = np.zeros(16000 * 12, dtype=np.float32)
    segs = [{"start": 0, "end": 3}, {"start": 3, "end": 6}, {"start": 6, "end": 9}]
    assert media._diarize(audio, segs) == ["Speaker 1", "Speaker 2", "Speaker 1"]


def test_diarize_single_speaker(monkeypatch):
    pytest.importorskip("scipy")  # clustering uses scipy; CI strips the ML stack
    import server.diarization.speakers as sp

    v = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    monkeypatch.setattr(sp, "embed", lambda clip: v.copy())
    audio = np.zeros(16000 * 9, dtype=np.float32)
    segs = [{"start": 0, "end": 3}, {"start": 3, "end": 6}, {"start": 6, "end": 9}]
    assert media._diarize(audio, segs) == ["Speaker 1", "Speaker 1", "Speaker 1"]


def test_diarize_falls_back_when_no_embeddings(monkeypatch):
    import server.diarization.speakers as sp

    monkeypatch.setattr(sp, "embed", lambda clip: None)  # clips too short
    segs = [{"start": 0, "end": 1}, {"start": 1, "end": 2}]
    assert media._diarize(np.zeros(32000, dtype=np.float32), segs) == ["Speaker 1", "Speaker 1"]


def test_media_upload_routes_to_transcription(monkeypatch):
    app = FastAPI()
    app.include_router(A.router)
    client = TestClient(app)

    monkeypatch.setattr(
        A,
        "extract_media",
        lambda content, ext, filename: ("# Transcript\n\n[00:00] Speaker 1: hello world", []),
    )
    r = client.post("/api/upload", files={"files": ("clip.mp3", b"FAKEAUDIO", "audio/mpeg")})
    assert r.status_code == 200, r.text
    item = r.json()["attachments"][0]
    assert item["type"] == "mp3"
    assert item["frames"] == 0  # audio never retains frames
    att = A.attachments[item["id"]]
    assert att["kind"] == "document"
    assert "hello world" in att["text"]
    assert "frames" not in att  # no empty frames key for audio
    # Transcript headings feed the outline / section-fetch path.
    assert att.get("outline")


def test_video_upload_retains_frames(monkeypatch):
    app = FastAPI()
    app.include_router(A.router)
    client = TestClient(app)

    frames = [
        {"data": "AAAA", "media_type": "image/jpeg", "timestamp": 0.0},
        {"data": "BBBB", "media_type": "image/jpeg", "timestamp": 10.0},
    ]
    monkeypatch.setattr(
        A,
        "extract_media",
        lambda content, ext, filename: (
            "# Transcript of clip.mp4\n\n[00:00] Speaker 1: hi",
            frames,
        ),
    )
    r = client.post("/api/upload", files={"files": ("clip.mp4", b"FAKEVIDEO", "video/mp4")})
    assert r.status_code == 200, r.text
    item = r.json()["attachments"][0]
    assert item["type"] == "mp4"
    assert item["frames"] == 2
    att = A.attachments[item["id"]]
    # Stored as a document (transcript flows through the outline path) but the
    # retained frames ride along for the chat layer to emit as image blocks.
    assert att["kind"] == "document"
    assert att["frames"] == frames
