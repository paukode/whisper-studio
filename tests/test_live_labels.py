"""Speaker labels follow the sentence (server/diarization/live.py).

Field logs: labelling a sentence before sending it held the transcript back
by the speaker model's load (3 to 4 s on the first sentence) and by every
handover check (about 2 s each), and while it ran the socket read no audio.
These drive the real /ws handler with a scripted engine and a scripted
speaker session: the sentence must reach the client before its label is
known, audio must keep flowing while labelling is stuck, and the label (or a
handover split) must still arrive, and arrive before a stop completes.
"""

import os
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.websocket as ws_mod
from server import diarization, lost_audio

_PCM = b"\x00\x00" * 1600


def _final(text: str) -> dict:
    return {"kind": "final", "text": text, "audio": np.zeros(16000, dtype=np.float32)}


class _Session:
    def __init__(self, script):
        self._script = script

    def process(self, raw_pcm):
        return self._script.pop(0) if self._script else []

    def finish(self):
        return []

    def close(self):
        pass


class _Backend:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="labels-asr")
        self.script: list[list[dict]] = []
        self.warmed = threading.Event()

    def is_loaded(self):
        return True

    def load(self):
        pass

    def warm(self):
        self.warmed.set()

    def create_session(self):
        session, self.script = _Session(self.script), []
        return session


class _Speakers:
    """A speaker session whose next label the test decides."""

    def __init__(self):
        self.label = "Speaker 1"
        self.assigned: list[int] = []

    def fallback_label(self):
        return "Speaker 1"

    def assign(self, chunk_id, embedding, duration=None, text=""):
        self.assigned.append(chunk_id)
        return self.label

    def maybe_recluster(self):
        return {}

    def take_new_names(self):
        return {}

    def known_names(self):
        return {}

    def set_expected(self, count):
        pass


@dataclass
class _Span:
    text: str
    audio: np.ndarray
    embedding: np.ndarray | None = None
    overlap: bool = False


@pytest.fixture
def harness(monkeypatch):
    backend = _Backend()
    speakers = _Speakers()
    gate = threading.Event()
    gate.set()
    conf = {
        "transcription_backend": "streaming",
        "translate_mode": "off",
        "speaker_split_turns": False,
    }

    def embed(audio):
        gate.wait(10)
        return np.ones(4, dtype=np.float32)

    monkeypatch.setattr(ws_mod, "get_backend", lambda name: backend)
    monkeypatch.setattr(ws_mod, "config_get", lambda key, default=None: conf.get(key, default))
    monkeypatch.setattr(diarization, "embed", embed)
    monkeypatch.setattr(diarization, "get_session", lambda sid: speakers)
    monkeypatch.setattr(diarization, "drop_session", lambda sid: None)
    ws_mod._chunk_counters.clear()
    ws_mod._carryover.clear()
    app = FastAPI()
    app.include_router(ws_mod.router)
    with TestClient(app, base_url="http://localhost") as client:
        yield client, backend, speakers, gate, conf
    gate.set()
    ws_mod._chunk_counters.clear()
    ws_mod._carryover.clear()
    backend.executor.shutdown(wait=True)


def _until(ws, kind):
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == kind:
            return frames


def test_a_sentence_is_sent_before_its_label_and_audio_keeps_flowing(harness):
    client, backend, speakers, gate, _ = harness
    gate.clear()  # the speaker model is "loading": labelling is stuck
    speakers.label = "Speaker 2"
    backend.script = [[_final("Good afternoon Marcin.")], [{"kind": "interim", "text": "I can't"}]]
    with client.websocket_connect("/ws?session_id=labels-1&take=t1") as ws:
        ws.send_bytes(_PCM)
        sent = _until(ws, "transcript")[-1]
        assert sent["text"] == "Good afternoon Marcin." and sent["speaker"] == "Speaker 1"
        # Labelling is still stuck, yet the next chunk of audio is read and
        # its draft comes back.
        ws.send_bytes(_PCM)
        assert _until(ws, "interim")[-1]["text"] == "I can't"
        gate.set()
        update = _until(ws, "speaker_update")[-1]
        assert update["updates"] == [{"chunk_id": sent["chunk_id"], "speaker": "Speaker 2"}]


def test_a_handover_inside_a_sent_sentence_arrives_as_a_split(harness, monkeypatch):
    client, backend, speakers, _, conf = harness
    conf["speaker_split_turns"] = True
    labels = iter(["Speaker 1", "Speaker 2"])
    monkeypatch.setattr(speakers, "assign", lambda cid, emb, dur=None, text="": next(labels))
    audio = np.zeros(8000, dtype=np.float32)
    monkeypatch.setattr(
        diarization.turns,
        "split_utterance",
        lambda a, w: [_Span("Right, my point.", audio), _Span("But wait.", audio, overlap=True)],
    )
    backend.script = [[_final("Right, my point. But wait.")]]
    with client.websocket_connect("/ws?session_id=labels-2&take=t2") as ws:
        ws.send_bytes(_PCM)
        sent = _until(ws, "transcript")[-1]
        split = _until(ws, "transcript_split")[-1]
    first = sent["chunk_id"]
    assert split["chunk_id"] == first
    assert split["parts"] == [
        {"chunk_id": first, "text": "Right, my point.", "speaker": "Speaker 1"},
        {"chunk_id": first + 1, "text": "But wait.", "speaker": "Speaker 2", "overlap": True},
    ]


def test_stop_waits_for_every_label(harness):
    client, backend, speakers, gate, _ = harness
    gate.clear()
    speakers.label = "Speaker 2"
    backend.script = [[_final("One.")]]
    with client.websocket_connect("/ws?session_id=labels-3&take=t3") as ws:
        ws.send_bytes(_PCM)
        _until(ws, "transcript")
        ws.send_json({"type": "stop"})
        threading.Timer(0.3, gate.set).start()
        frames = _until(ws, "session_ended")
    kinds = [f["type"] for f in frames]
    assert kinds.index("speaker_update") < kinds.index("session_ended")


def test_the_engine_is_warmed_when_a_recording_connects(harness):
    client, backend, *_ = harness
    with client.websocket_connect("/ws?session_id=labels-4&take=t4") as ws:
        ws.send_json({"type": "ping"})
        _until(ws, "pong")
    assert backend.warmed.wait(2)


@pytest.mark.parametrize("keep", [False, True])
def test_lost_sentence_audio_is_kept_only_when_asked(harness, monkeypatch, tmp_path, keep):
    client, backend, _, _, conf = harness
    monkeypatch.setattr(lost_audio, "directory", lambda: str(tmp_path / "lost"))
    monkeypatch.setattr(lost_audio, "enabled", lambda: keep)
    pcm = (np.ones(4000, dtype=np.int16) * 500).tobytes()
    why = {
        "draft": "to be done by November",
        "reason": "no words in the final decode",
        "decoded": [""],
        "pcm": pcm,
    }
    backend.script = [
        [{"kind": "interim", "text": "to be done by November"}],
        [{"kind": "interim", "text": "", "withdrawn": why}],
    ]
    with client.websocket_connect("/ws?session_id=labels-5&take=t5") as ws:
        ws.send_bytes(_PCM)
        ws.send_bytes(_PCM)
        frames = _until(ws, "interim") + _until(ws, "interim")
        assert frames[-1] == {"type": "interim", "text": ""}  # the wire never carries audio
        ws.send_json({"type": "ping"})
        _until(ws, "pong")
    folder = tmp_path / "lost"
    deadline = time.monotonic() + 2
    while keep and time.monotonic() < deadline and not (folder.exists() and any(folder.iterdir())):
        time.sleep(0.02)
    wavs = sorted(folder.glob("*.wav")) if folder.exists() else []
    if not keep:
        assert wavs == []
        return
    assert len(wavs) == 1
    with wave.open(str(wavs[0])) as w:
        assert (w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())) == (16000, 1, pcm)
    assert open(os.path.splitext(wavs[0])[0] + ".txt").read().strip() == "to be done by November"


def test_lost_audio_keeps_only_the_newest_files(monkeypatch, tmp_path):
    monkeypatch.setattr(lost_audio, "directory", lambda: str(tmp_path))
    monkeypatch.setattr(lost_audio, "KEEP_FILES", 3)
    paths = []
    for i in range(5):
        paths.append(lost_audio.save(b"\x00\x00" * 160, f"take{i}", f"draft {i}"))
        time.sleep(0.002)
    left = sorted(p.name for p in tmp_path.glob("*.wav"))
    assert len(left) == 3 and all(os.path.basename(p) in left for p in paths[-3:])
