"""Near-cap wind-down and the token-aware context store."""

import pytest

from server.chat import loop_hints


@pytest.fixture(autouse=True)
def clean_store():
    with loop_hints._lock:
        loop_hints._context.clear()
    yield


def test_reminder_thresholds():
    assert loop_hints.near_cap_reminder(50) is None
    assert loop_hints.near_cap_reminder(6) is None
    assert "5 tool rounds remain" in loop_hints.near_cap_reminder(5)
    assert loop_hints.near_cap_reminder(4) is None
    assert "final tool round" in loop_hints.near_cap_reminder(1)
    assert loop_hints.near_cap_reminder(0) is None


def test_inject_reminder_appends_text_block():
    messages = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]},
    ]
    assert loop_hints.inject_reminder(messages, "<system-reminder>wind down</system-reminder>")
    assert messages[0]["content"][-1] == {
        "type": "text",
        "text": "<system-reminder>wind down</system-reminder>",
    }


def test_inject_reminder_converts_string_content_in_place():
    messages = [{"role": "user", "content": "original question"}]
    assert loop_hints.inject_reminder(messages, "note")
    assert messages[0]["content"][0] == {"type": "text", "text": "original question"}
    assert messages[0]["content"][1] == {"type": "text", "text": "note"}


def test_inject_reminder_refuses_non_user_tail():
    messages = [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
    assert loop_hints.inject_reminder(messages, "note") is False
    assert len(messages[0]["content"]) == 1


def test_context_store_roundtrip_and_estimate():
    assert loop_hints.context_estimate("s1") == (None, loop_hints.DEFAULT_CONTEXT_MAX)
    loop_hints.note_prompt_tokens("s1", 120_000, 200_000)
    assert loop_hints.context_estimate("s1") == (120_000, 200_000)
    # Latest round wins (context is a level, not a sum).
    loop_hints.note_prompt_tokens("s1", 130_000, 200_000)
    assert loop_hints.context_estimate("s1")[0] == 130_000


def test_compaction_nudge_fires_once_at_threshold():
    loop_hints.note_prompt_tokens("s2", 100_000, 200_000)
    assert loop_hints.should_nudge_compaction("s2") is False
    loop_hints.note_prompt_tokens("s2", 165_000, 200_000)  # >80%
    assert loop_hints.should_nudge_compaction("s2") is True
    assert loop_hints.should_nudge_compaction("s2") is False  # one-shot


def test_store_bounded():
    for i in range(loop_hints._MAX_SESSIONS + 10):
        loop_hints.note_prompt_tokens(f"s{i}", 100)
    assert len(loop_hints._context) <= loop_hints._MAX_SESSIONS


def test_eviction_is_lru_by_update_not_fifo():
    """An actively-updated session must never be the eviction victim."""
    loop_hints.note_prompt_tokens("busy", 100)
    for i in range(loop_hints._MAX_SESSIONS - 1):
        loop_hints.note_prompt_tokens(f"idle{i}", 100)
    # Store is full; refresh the oldest-inserted session, then overflow.
    loop_hints.note_prompt_tokens("busy", 200)
    loop_hints.note_prompt_tokens("newcomer", 100)
    assert loop_hints.context_estimate("busy")[0] == 200  # survived
    assert loop_hints.context_estimate("idle0")[0] is None  # evicted instead
