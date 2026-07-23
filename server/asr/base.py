"""ASR backend contract.

Every backend is a self-contained module exposing:

    executor: concurrent.futures.ThreadPoolExecutor
        The thread(s) its sessions must run on. Parakeet needs a single
        dedicated thread (MLX streams are thread-local); Whisper uses its
        own small pool. The orchestrator (server/websocket.py) routes every
        session call through this executor and never runs model code on
        the event loop.

    create_session() -> AsrSession
        Build one per-connection decoding session. Called on ``executor``
        so model load/warmup happens on the right thread.

    preload() -> None
        Optional eager model load for startup warmup. Best-effort.

Sessions are duck-typed to the protocol below. Events are plain dicts:

    {"kind": "interim", "text": str}
        Volatile word-by-word draft of the in-flight utterance. May
        self-correct; carries no audio and never gets a speaker label.
        Backends without live drafts simply never emit it.

    {"kind": "final", "text": str, "audio": np.ndarray}
        A settled utterance at a natural silence boundary. ``audio`` is
        the float32 mono 16 kHz window the text was decoded from, handed
        back so the caller can run speaker identification on it. The
        backend itself never touches diarization.

Adding a backend: drop a module in this package and register it in
``BACKENDS`` (see __init__.py). Removing one: delete the module and its
registry line — nothing else imports backend internals.
"""

from __future__ import annotations

from typing import Protocol


class AsrSession(Protocol):
    def process(self, raw_pcm: bytes) -> list[dict]:
        """Feed one PCM16 mono 16 kHz chunk; return ordered events."""
        ...

    def finish(self) -> list[dict]:
        """Flush any in-flight utterance as final (stop / backend switch)."""
        ...

    def close(self) -> None:
        """Release decoder state (disconnect)."""
        ...
