"""Verify auto-mode's classify_tool_call does not block the event loop.

The classifier makes a blocking boto3 `bedrock.invoke_model` call. It is awaited
on the main event loop during a chat stream (from tool_executor), so it MUST be
offloaded to a thread executor (run_in_executor) rather than called inline,
otherwise every classification freezes all SSE streams / the ASR websocket for
the full Haiku round-trip. This test proves the blocking call runs on a
different thread than the event loop, and that behavior (the allow/confirm
parse) is preserved.
"""

import asyncio
import json
import threading

from server import auto_mode


class _FakeBody:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload


class _FakeBedrock:
    def __init__(self, record: dict):
        self._record = record

    def invoke_model(self, **kwargs):
        # Record the thread the blocking call runs on.
        self._record["invoke_thread"] = threading.get_ident()
        text = json.dumps({"decision": "allow", "reason": "safe read"})
        payload = json.dumps({"content": [{"text": text}]}).encode()
        return {"body": _FakeBody(payload)}


def test_classify_tool_call_offloads_blocking_invoke(monkeypatch):
    import boto3

    record: dict = {}
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakeBedrock(record))

    config = {"chat_models": {"haiku": "test-haiku"}}

    async def _run():
        record["loop_thread"] = threading.get_ident()
        return await auto_mode.classify_tool_call("Read", {"path": "foo.txt"}, config, "test-model")

    result = asyncio.run(_run())

    # Behavior preserved: the allow decision is parsed and returned unchanged.
    assert result == {"decision": "allow", "reason": "safe read"}

    # The blocking invoke ran, and it ran OFF the event-loop thread
    # (i.e. it was dispatched via run_in_executor, not inline on the loop).
    assert "invoke_thread" in record, "invoke_model was never called"
    assert record["invoke_thread"] != record["loop_thread"]
