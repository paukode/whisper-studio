"""The local adapter's one retry for a reply that only announces a step.

Measured 2026-10-23: Ministral 3 14B answered "can you review this code for
me?" with "Let me first list the files ... I'll then read and analyze them"
and no tool call, which ends the turn with nothing done because the loop only
resumes a model after a call. The adapter now asks once more, holding the
retry's output until it proves to be a call.
"""

import asyncio

import server.local.server_stream as SS
from server.chat.engine.events import RoundResult, TextDelta, ToolCall
from server.chat.engine.local import _ANNOUNCE_NUDGE, LocalAdapter, _announces_next_step
from tests.test_engine_local import _assert_alternates, _fake_rounds, _run_local_turn

ANNOUNCE = "Let me first list the files in your workspace. I'll then read and analyze them."
LIST_CALL = {"id": "call_1", "name": "ws_list_directory", "input": {"path": "."}}
TOOLS = [
    {
        "name": "ws_list_directory",
        "description": "List a directory.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
]


def _round(monkeypatch, rounds, *, tools=TOOLS, is_last_round=False):
    """One adapter round on scripted attempts: (events, fake)."""
    fake = _fake_rounds(*rounds)
    monkeypatch.setattr(SS, "_stream_round", fake)
    adapter = LocalAdapter(
        model_key="local_gemma",
        base_url="http://127.0.0.1:9999",
        system_prompt="sys",
        thinking_level=None,
        tools_enabled=True,
    )
    messages = [{"role": "user", "content": "can you review this code for me?"}]

    async def go():
        return [ev async for ev in adapter.stream_round(messages, tools, 1, 0, is_last_round)]

    return asyncio.run(go()), fake


def _shown(events) -> str:
    return "".join(ev.text for ev in events if isinstance(ev, TextDelta))


def test_an_announced_step_is_retried_once_and_the_call_is_made(monkeypatch):
    events, fake = _round(monkeypatch, [([("text", ANNOUNCE)], []), ([], [LIST_CALL])])

    assert len(fake.payloads) == 2
    retry = fake.payloads[1]["messages"]
    _assert_alternates(retry)
    # The model's own words go back as its turn, then the nudge.
    assert retry[-2] == {"role": "assistant", "content": ANNOUNCE}
    assert retry[-1] == {"role": "user", "content": _ANNOUNCE_NUDGE}
    assert [ev.name for ev in events if isinstance(ev, ToolCall)] == ["ws_list_directory"]
    # The user sees the announcement once, and the round records it with the call.
    assert _shown(events) == ANNOUNCE
    result = events[-1]
    assert isinstance(result, RoundResult)
    assert result.stop_reason == "tool_use"
    assert result.content[0] == {"type": "text", "text": ANNOUNCE}
    assert result.content[-1]["type"] == "tool_use"


def test_prose_from_the_retry_is_dropped_and_the_announcement_stands(monkeypatch):
    events, fake = _round(
        monkeypatch,
        [([("text", ANNOUNCE)], []), ([("text", "Here is my full review of the code.")], [])],
    )

    assert len(fake.payloads) == 2
    assert _shown(events) == ANNOUNCE
    result = events[-1]
    assert result.stop_reason == "end_turn"
    assert result.content == [{"type": "text", "text": ANNOUNCE}]


def test_the_retry_preamble_is_shown_with_its_call(monkeypatch):
    events, _fake = _round(
        monkeypatch,
        [([("text", ANNOUNCE)], []), ([("text", "Listing the root now.")], [LIST_CALL])],
    )

    assert _shown(events) == f"{ANNOUNCE}\n\nListing the root now."
    assert events[-1].content[0]["text"] == f"{ANNOUNCE}\n\nListing the root now."


def test_usage_counts_both_attempts_once(monkeypatch):
    first = {
        "prompt_tokens": 900,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    second = {
        "prompt_tokens": 950,
        "completion_tokens": 15,
        "prompt_tokens_details": {"cached_tokens": 920},
    }
    events, _fake = _round(
        monkeypatch, [([("text", ANNOUNCE)], [], first), ([], [LIST_CALL], second)]
    )

    usage = events[-1].usage
    assert usage.output_tokens == 35
    assert usage.cache_read_tokens == 920
    assert usage.input_tokens == 900 + (950 - 920)


def test_a_question_or_a_request_for_input_is_a_real_stop(monkeypatch):
    for text in (
        "Which file should I review first?",
        "Sure. Please provide the file path, and I'll review it.",
        "I'll review it once you let me know which part matters most.",
    ):
        _events, fake = _round(monkeypatch, [([("text", text)], [])])
        assert len(fake.payloads) == 1, text


def test_no_retry_when_tools_cannot_be_called(monkeypatch):
    _events, fake = _round(monkeypatch, [([("text", ANNOUNCE)], [])], tools=[])
    assert len(fake.payloads) == 1
    _events, fake = _round(monkeypatch, [([("text", ANNOUNCE)], [])], is_last_round=True)
    assert len(fake.payloads) == 1


def test_a_long_answer_is_never_taken_for_an_announcement():
    answer = "I'll summarize the bugs I found. " + "The menu route reads a missing field. " * 20
    assert not _announces_next_step(answer)
    assert _announces_next_step(ANNOUNCE)
    assert _announces_next_step("To get started, I'll list the files and directories.")
    assert not _announces_next_step("")


def test_a_full_turn_continues_after_the_retried_call(monkeypatch):
    """On the engine: the call runs through the shared gate and the turn goes
    on to the model's answer, instead of ending after the announcement."""
    capture: dict = {}
    blob, fake = _run_local_turn(
        monkeypatch,
        [
            ([("text", ANNOUNCE)], []),
            ([], [LIST_CALL]),
            ([("text", "The menu route reads item.name, which does not exist.")], []),
        ],
        messages=[{"role": "user", "content": "can you review this code for me?"}],
        tools_enabled=True,
        capture=capture,
    )

    assert [t["name"] for t in capture["tool_uses"]] == ["ws_list_directory"]
    assert "item.name, which does not exist" in blob
    assert blob.count("Let me first list the files") == 1
    assert len(fake.payloads) == 3
