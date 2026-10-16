"""Keep the audio of sentences the recorder lost, for diagnosis (opt-in).

A live draft can show words and then leave the screen because the final
decode of the same sentence came back empty. Field logs showed 55 of those in
one 27-minute meeting, real phrases among them, and clean test speech does
not reproduce it, so the cause has to be found in the real audio. With
``keep_lost_sentence_audio`` on, each such sentence is written as a WAV (16 kHz
mono PCM16) under ``<data root>/lost_sentences/``, named by time and take,
with the lost draft in a sidecar .txt. Only the newest KEEP_FILES are kept.
"""

from __future__ import annotations

import logging
import os
import time
import wave

from server.infrastructure.config import get as config_get
from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

DIR_NAME = "lost_sentences"
KEEP_FILES = 200
SAMPLE_RATE = 16000


def enabled() -> bool:
    return bool(config_get("keep_lost_sentence_audio"))


def directory() -> str:
    return os.path.join(data_root(), DIR_NAME)


def save(pcm: bytes, take_tag: str, draft: str) -> str:
    """Write one lost sentence and prune the oldest beyond KEEP_FILES.
    Returns the WAV path. Blocking file IO: call off the event loop."""
    folder = directory()
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    safe_tag = "".join(c for c in take_tag if c.isalnum()) or "rec"
    base = os.path.join(folder, f"{stamp}-{safe_tag}")
    with wave.open(base + ".wav", "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm[: len(pcm) - len(pcm) % 2])
    with open(base + ".txt", "w", encoding="utf-8") as note:
        note.write(draft + "\n")
    _prune(folder)
    return base + ".wav"


def _prune(folder: str) -> None:
    wavs = sorted(f for f in os.listdir(folder) if f.endswith(".wav"))
    for name in wavs[: max(0, len(wavs) - KEEP_FILES)]:
        for path in (os.path.join(folder, name), os.path.join(folder, name[:-4] + ".txt")):
            try:
                os.remove(path)
            except OSError as e:
                log.debug("Could not prune %s: %s", path, e)
