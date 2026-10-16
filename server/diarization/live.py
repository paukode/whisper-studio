"""Speaker labels for a live recording, decided after each sentence is sent.

The transcription socket used to label a sentence before sending it: embed
it, try to cut it at a speaker handover, then send. On a busy or swapping
Mac that took seconds per sentence (the speaker model loading on the first
sentence, about 2 s for every handover check), and while it ran the socket
read no audio at all, so the whole transcript fell behind live speech.

Now the socket sends each final at once under a provisional label (the
speaker of the last labelled sentence) and hands it to this labeller, one
per connection, which works through the sentences in order on its own task:

- one speaker: assign the label; if it differs from the provisional one,
  send ``speaker_update`` for that chunk (the client already applies these);
- a handover inside the sentence: send ``transcript_split``, which replaces
  the sentence's chunk with one chunk per speaker turn (the first keeps the
  original chunk id, the rest take new ids from the socket's counter);
- then the periodic re-cluster and any newly recognised names, as before.

``drain`` lets `stop` wait for every label before the client saves.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import numpy as np

from server import diarization
from server.diarization.speakers import SpeakerSession

log = logging.getLogger("whisper-studio")

SAMPLE_RATE = 16000


@dataclass
class Pending:
    """One sent final waiting for its speaker label."""

    chunk_id: int
    text: str
    audio: np.ndarray | None
    words: list[dict] | None
    provisional: str
    sent_at: float = field(default_factory=time.monotonic)


class LiveLabeller:
    def __init__(
        self,
        speakers: SpeakerSession,
        *,
        split_turns: bool,
        send: Callable[[dict], Awaitable[None]],
        new_chunk_id: Callable[[], int],
        on_labelled: Callable[[Pending, list[str], float], None] | None = None,
    ) -> None:
        self._speakers = speakers
        self._split_turns = split_turns
        self._budget = diarization.turns.SplitBudget()
        self._send = send
        self._new_chunk_id = new_chunk_id
        self._on_labelled = on_labelled
        self._queue: asyncio.Queue[Pending] = asyncio.Queue()
        self._worker: asyncio.Task | None = None

    def provisional_label(self) -> str:
        """The label a sentence is sent under before it is labelled."""
        return self._speakers.fallback_label()

    def submit(self, pending: Pending) -> None:
        self._queue.put_nowait(pending)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def drain(self) -> None:
        """Wait until every submitted sentence is labelled."""
        await self._queue.join()

    def cancel(self) -> None:
        """The connection is gone: unlabelled sentences keep their
        provisional labels on the client."""
        if self._worker is not None:
            self._worker.cancel()

    async def _run(self) -> None:
        while True:
            pending = await self._queue.get()
            try:
                await self._label(pending)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Speaker labelling failed for chunk %s: %s", pending.chunk_id, e)
            finally:
                self._queue.task_done()

    async def _label(self, pending: Pending) -> None:
        loop = asyncio.get_running_loop()
        spans = []
        if self._split_turns and self._budget.open and pending.audio is not None:
            # Looked up through the package at call time: the seams tests
            # (and the socket) patch.
            spans = await loop.run_in_executor(
                diarization.executor,
                diarization.turns.split_utterance,
                pending.audio,
                pending.words,
            )
            self._budget.record(len(spans) > 1)
        if len(spans) > 1:
            log.info("Speaker handover split one utterance into %d turns", len(spans))
            parts = []
            for span in spans:
                if not span.text:
                    continue
                chunk_id = pending.chunk_id if not parts else self._new_chunk_id()
                label = await self._assign(chunk_id, span.embedding, span.audio, span.text)
                part = {"chunk_id": chunk_id, "text": span.text, "speaker": label}
                if span.overlap:
                    part["overlap"] = True
                parts.append(part)
            if parts:
                await self._send(
                    {"type": "transcript_split", "chunk_id": pending.chunk_id, "parts": parts}
                )
            labels = [p["speaker"] for p in parts]
        else:
            label = await self._assign(pending.chunk_id, None, pending.audio, pending.text)
            if label != pending.provisional:
                await self._send(
                    {
                        "type": "speaker_update",
                        "updates": [{"chunk_id": pending.chunk_id, "speaker": label}],
                    }
                )
            labels = [label]
        if self._on_labelled is not None:
            self._on_labelled(pending, labels, time.monotonic() - pending.sent_at)
        # Periodic self-heal: re-cluster everything seen so far and retro-fix
        # the few sentences whose label changed.
        updates = await loop.run_in_executor(diarization.executor, self._speakers.maybe_recluster)
        if updates:
            await self._send(
                {
                    "type": "speaker_update",
                    "updates": [{"chunk_id": cid, "speaker": lab} for cid, lab in updates.items()],
                }
            )
        names = self._speakers.take_new_names()
        if names:
            await self._send({"type": "speaker_named", "names": names})

    async def _assign(
        self, chunk_id: int, embedding: np.ndarray | None, audio: np.ndarray | None, text: str
    ) -> str:
        if embedding is None and audio is not None:
            embedding = await asyncio.get_running_loop().run_in_executor(
                diarization.executor, diarization.embed, audio
            )
        if embedding is None:
            # Too short or a failed embed: speaker continuity is the best
            # guess and never pollutes the cluster space.
            return self._speakers.fallback_label()
        duration = len(audio) / SAMPLE_RATE if audio is not None else None
        return self._speakers.assign(chunk_id, embedding, duration, text)
