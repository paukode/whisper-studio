"""Bedrock stream chunks reach the event loop as they arrive.

The adapter reads botocore's blocking EventStream on a worker thread and hands
each chunk to the loop through an asyncio.Queue. A bare ``put_nowait`` from
that thread resolves the waiting getter without waking the loop, so a chunk
sat until something unrelated woke it (uvicorn's tick in production, the end
of the stream here). The handoff must go through ``call_soon_threadsafe``.
"""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import server.chat.engine.anthropic as anth
from server.chat.engine.events import RoundResult, TextDelta


def _ev(obj):
    return {"chunk": {"bytes": json.dumps(obj).encode()}}


class _SlowBody:
    """A text stream producing a delta every 0.1 s, then holding the HTTP body
    open (no EOF yet) so the reader thread stays alive."""

    def __init__(self, stamps):
        self.stamps = stamps

    def __iter__(self):
        yield _ev({"type": "message_start", "message": {"usage": {"input_tokens": 1}}})
        yield _ev({"type": "content_block_start", "content_block": {"type": "text"}})
        for i in range(4):
            time.sleep(0.1)
            self.stamps[f"t{i} "] = time.monotonic()
            yield _ev(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": f"t{i} "}}
            )
        yield _ev({"type": "content_block_stop"})
        yield _ev(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 4},
            }
        )
        time.sleep(1.5)

    def close(self):
        pass


def test_each_delta_is_yielded_when_it_is_produced(monkeypatch):
    stamps: dict[str, float] = {}
    seen: dict[str, float] = {}

    class _Client:
        def invoke_model_with_response_stream(self, **kw):
            return {"body": _SlowBody(stamps)}

    monkeypatch.setattr(anth, "_get_bedrock_client", lambda: _Client())

    async def go():
        adapter = anth.AnthropicAdapter(
            model_key="sonnet",
            model_id="us.anthropic.claude-sonnet-4-5",
            system_prompt="s",
            system_static="",
            system_dynamic="",
            caching_on=False,
            cache_ttl="5m",
            effort_label=None,
            force_skill=None,
            loop=asyncio.get_running_loop(),
            executor=ThreadPoolExecutor(max_workers=4),
            meta={},
        )
        monkeypatch.setattr(adapter, "_build_body", lambda *a, **k: {})
        async for ev in adapter.stream_round([], [], 0, 0, False):
            if isinstance(ev, TextDelta):
                seen[ev.text] = time.monotonic()
            elif isinstance(ev, RoundResult):
                return ev

    result = asyncio.run(go())
    assert result is not None and set(seen) == set(stamps)
    # Nothing else wakes this loop, so a lost wakeup would hold every delta
    # until the reader thread ends 1.5 s later.
    lags = [seen[text] - stamps[text] for text in stamps]
    assert max(lags) < 0.4, lags
