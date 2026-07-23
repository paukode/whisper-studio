"""Cooperative cancellation of on-device generation.

Local turns generate on a SINGLE model thread (server/local/runtime.executor),
so a round abandoned by a client disconnect / Stop must be told to stop or it
decodes on to ``max_tokens`` and wedges the next turn. The fix threads a
``threading.Event`` into ``iter_generate_round`` / ``iter_chat``; the async SSE
bridge in server/local/stream.py sets it from a ``finally`` when its generator is
torn down, and the token loop checks the flag and breaks.

These tests exercise that plumbing with a FAKE generator (no GGUF, no
llama-cpp-python) that yields tokens while polling the stop signal and records
whether it saw the signal and broke early — so they stay hermetic and fast.
"""

import asyncio
import threading

import server.local.runtime as L
from server.local.stream import _stream_round, stream_local_chat

# A generous per-fake token budget: far more than the handful the fake emits
# before cancellation lands, so "ran to completion" is an unambiguous failure
# signal. Each simulated "decode" waits at most _TOK_LATENCY_S, and a set flag
# returns immediately, so the loop breaks within a tick of the cancel.
_FAKE_MAX_TOKENS = 50
_TOK_LATENCY_S = 0.02


def _make_blocking_fake():
    """Build a fake generation round plus a recorder + completion event.

    The returned generator yields one token immediately (so the consumer can
    advance and then tear the stream down), then simulates a long decode: each
    "token" waits briefly on the cancel flag, exactly like the real loop's
    per-token check. A set flag breaks the loop and is recorded."""
    rec = {"saw_cancel": False, "ran_to_completion": False, "produced": 0}
    finished = threading.Event()

    def fake(*args, cancel=None, **kwargs):
        try:
            yield ("text", "tok0")
            for i in range(1, _FAKE_MAX_TOKENS):
                # cancel.wait doubles as the per-token decode latency AND the
                # cooperative stop check: a set event returns True at once.
                if cancel is not None and cancel.wait(timeout=_TOK_LATENCY_S):
                    rec["saw_cancel"] = True
                    break
                rec["produced"] = i
                yield ("text", f"tok{i}")
            else:
                rec["ran_to_completion"] = True
            yield ("raw", "tok0" + "".join(f"tok{i}" for i in range(1, rec["produced"] + 1)))
        finally:
            finished.set()

    return fake, rec, finished


async def _wait_for(event: threading.Event, timeout: float = 5.0) -> bool:
    """Poll a cross-thread Event without blocking the event loop (so the
    producer's call_soon_threadsafe callbacks keep draining)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if event.is_set():
            return True
        await asyncio.sleep(0.01)
    return event.is_set()


def test_stream_round_cancels_producer_on_close(monkeypatch):
    """Tearing down _stream_round (client disconnect / Stop) must signal the
    producer so the fake generation stops early instead of decoding its whole
    budget — proving the single model thread is freed promptly."""
    fake, rec, finished = _make_blocking_fake()
    monkeypatch.setattr(L, "iter_generate_round", fake)

    async def drive():
        agen = _stream_round("local_gemma", [], [])
        first = await agen.__anext__()
        assert first == ("text", "tok0")  # streaming is live
        await agen.aclose()  # GeneratorExit -> finally -> cancel.set()
        # The producer runs on the real single-thread executor; wait for it to
        # observe the flag and unwind.
        assert await _wait_for(finished), "producer thread did not stop after cancel"

    asyncio.run(drive())

    assert rec["saw_cancel"] is True  # the token loop saw the stop signal...
    assert rec["ran_to_completion"] is False  # ...and broke instead of finishing
    assert rec["produced"] < _FAKE_MAX_TOKENS - 1


def test_plain_chat_cancels_producer_on_close(monkeypatch):
    """The plain / thinking path (stream_local_chat, tools off) pumps iter_chat
    through the same queue bridge and must cancel it on teardown too."""
    fake, rec, finished = _make_blocking_fake()
    monkeypatch.setattr(L, "iter_chat", fake)

    async def drive():
        agen = stream_local_chat(
            "local_gemma", "sys", [{"role": "user", "content": "hi"}], "sess-cancel"
        )
        first = await agen.__anext__()
        assert '"text": "tok0"' in first  # first token streamed as SSE
        await agen.aclose()
        assert await _wait_for(finished), "producer thread did not stop after cancel"

    asyncio.run(drive())

    assert rec["saw_cancel"] is True
    assert rec["ran_to_completion"] is False
    assert rec["produced"] < _FAKE_MAX_TOKENS - 1


def test_stream_round_happy_path_runs_to_completion(monkeypatch):
    """No behavior change when nobody cancels: a fully drained round runs to
    completion, never trips the stop flag, and returns all its text + raw."""
    rec = {"saw_cancel": False}

    def fake(*args, cancel=None, **kwargs):
        for tok in ("Hello", " world"):
            if cancel is not None and cancel.is_set():
                rec["saw_cancel"] = True
                return
            yield ("text", tok)
        yield ("raw", "Hello world<|tool_call>...")

    monkeypatch.setattr(L, "iter_generate_round", fake)

    async def drive():
        return [piece async for piece in _stream_round("local_gemma", [], [])]

    pieces = asyncio.run(drive())

    assert ("text", "Hello") in pieces and ("text", " world") in pieces
    assert pieces[-1] == ("raw", "Hello world<|tool_call>...")
    assert rec["saw_cancel"] is False  # flag never set on a clean, full drain
