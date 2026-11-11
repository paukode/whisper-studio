"""Many sessions answer at once, and an answer never waits behind tool calls.

A Claude answer blocks a thread for as long as it streams. On the shared chat
pool, answers in flight held its workers, so once they filled it the next
answer, and every tool call queued behind them, waited with nothing on screen
until some other answer ended. Model calls now run on threads of their own
(server/infrastructure/own_thread.py), and the pool keeps only tool calls.
"""

import asyncio
import json
import threading
import time

import pytest

import server.chat.engine.anthropic as anth
from server.chat import executor as chat_pool
from server.chat.engine.events import RoundResult
from server.infrastructure.own_thread import run_on_own_thread

# The sessions the UI lets run at once (MAX_ACTIVE_SESSIONS in
# src/hooks/useChatStream.ts).
SESSIONS = 8


def _ev(obj):
    return {"chunk": {"bytes": json.dumps(obj).encode()}}


class _Body:
    """One short text answer that takes ``seconds`` to stream."""

    def __init__(self, seconds: float):
        self.seconds = seconds

    def __iter__(self):
        yield _ev({"type": "message_start", "message": {"usage": {"input_tokens": 1}}})
        yield _ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
        time.sleep(self.seconds)
        yield _ev(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "ok"},
            }
        )
        yield _ev({"type": "content_block_stop", "index": 0})
        yield _ev(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1},
            }
        )

    def close(self):
        pass


@pytest.fixture
def bedrock(monkeypatch):
    def install(seconds: float):
        class _Client:
            def invoke_model_with_response_stream(self, **kw):
                return {"body": _Body(seconds)}

        monkeypatch.setattr(anth, "_get_bedrock_client", lambda: _Client())

    return install


async def _answer(monkeypatch) -> RoundResult:
    adapter = anth.AnthropicAdapter(
        model_key="sonnet",
        model_id="us.anthropic.claude-sonnet-4-5",
        system_prompt="s",
        system_static="",
        system_dynamic="",
        caching_on=False,
        cache_ttl="5m",
        effort_label=None,
        meta={},
    )
    monkeypatch.setattr(adapter, "_build_body", lambda *a, **k: {})
    async for ev in adapter.stream_round([], [], 0, 0, False):
        if isinstance(ev, RoundResult):
            return ev
    raise AssertionError("the round ended without a result")


def test_an_answer_streams_while_every_tool_worker_is_busy(bedrock, monkeypatch):
    bedrock(0.0)
    release = threading.Event()
    busy = [chat_pool.submit(release.wait, 30) for _ in range(chat_pool._max_workers)]
    try:
        result = asyncio.run(asyncio.wait_for(_answer(monkeypatch), timeout=5))
    finally:
        release.set()
        for job in busy:
            job.result(timeout=5)

    assert result.stop_reason == "end_turn"


def test_every_session_streams_its_answer_at_the_same_time(bedrock, monkeypatch):
    bedrock(1.0)

    async def go():
        started = time.monotonic()
        results = await asyncio.gather(*(_answer(monkeypatch) for _ in range(SESSIONS)))
        return results, time.monotonic() - started

    results, elapsed = asyncio.run(go())

    assert [r.stop_reason for r in results] == ["end_turn"] * SESSIONS
    # One after the other they take SESSIONS seconds.
    assert elapsed < 3, elapsed


def test_the_thread_gives_back_its_result_or_its_error():
    def fail():
        raise ValueError("no answer")

    async def go():
        assert await run_on_own_thread(lambda: 42, name="probe") == 42
        with pytest.raises(ValueError, match="no answer"):
            await run_on_own_thread(fail, name="probe")

    asyncio.run(go())


def test_a_cancelled_wait_lets_the_thread_end_quietly():
    release = threading.Event()
    ended = threading.Event()

    def work():
        release.wait(5)
        ended.set()
        return "late"

    async def go():
        future = run_on_own_thread(work, name="probe")
        future.cancel()
        release.set()
        assert ended.wait(5)
        await asyncio.sleep(0.05)  # the settle callback runs on a done future
        return future.cancelled()

    assert asyncio.run(go())
