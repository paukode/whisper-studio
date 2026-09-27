"""An unexpected exception inside a model round ends the stream visibly.

The round loop only caught PromptTooLongError and WhisperAPIError, so any
other failure in an adapter (a malformed history row, a conversion bug)
escaped after the SSE headers were sent. The connection dropped with no error
frame and no [DONE], and the client could only show a bare network error.
"""

import asyncio
import json

import pytest

from server.chat.engine.events import TextDelta
from server.chat.engine.local import LocalAdapter
from server.chat.engine.policy import LOCAL_POLICY
from server.chat.engine.runner import TurnContext, run_turn


class _CrashingAdapter(LocalAdapter):
    def __init__(self, exc: BaseException):
        super().__init__(
            model_key="k",
            base_url="http://x",
            system_prompt="",
            thinking=False,
            tools_enabled=False,
        )
        self._exc = exc

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        yield TextDelta(text="partial ")
        raise self._exc


def _run(adapter):
    ctx = TurnContext(
        cost_source="chat",
        session_id="round-crash",
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": "q"}],
        adapter=adapter,
        policy=LOCAL_POLICY,
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
    )

    async def go():
        return [c async for c in run_turn(ctx)]

    return asyncio.run(go())


def _frames(chunks):
    out = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                out.append(line[6:])
    return out


def test_unexpected_round_exception_ends_with_the_error_frame():
    frames = _frames(_run(_CrashingAdapter(TypeError("can only join str, not NoneType"))))
    assert frames[-1] == "[DONE]" and frames.count("[DONE]") == 1
    errors = [json.loads(f)["error"] for f in frames[:-1] if '"error"' in f]
    assert errors and "NoneType" in errors[-1]
    # What had already streamed is still delivered ahead of the error.
    texts = [json.loads(f).get("text") for f in frames[:-1] if f.startswith("{")]
    assert "partial " in texts
    assert frames.index(json.dumps({"text": "partial "})) < len(frames) - 2


def test_blank_exception_message_still_names_the_failure():
    frames = _frames(_run(_CrashingAdapter(KeyError())))
    errors = [json.loads(f)["error"] for f in frames[:-1] if '"error"' in f]
    assert errors == ["KeyError"]


def test_cancellation_is_not_swallowed_as_an_error():
    with pytest.raises(asyncio.CancelledError):
        _run(_CrashingAdapter(asyncio.CancelledError()))
