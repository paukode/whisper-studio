"""The moving message checkpoint: request-only copy semantics."""

import copy

from server.chat.caching import annotate_messages_cache


def test_annotates_final_block_of_last_message():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "yo"}]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "out"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "out2"},
            ],
        },
    ]
    out = annotate_messages_cache(messages, "1h")
    assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # Earlier blocks and messages untouched.
    assert "cache_control" not in out[-1]["content"][0]
    assert "cache_control" not in out[0]["content"][0]


def test_never_mutates_the_shared_history():
    """The persisted history is replayed by the OpenAI/local paths where a
    stray cache_control key would fail the request — the original list must
    stay byte-identical."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "question"}]},
    ]
    snapshot = copy.deepcopy(messages)
    out = annotate_messages_cache(messages, "5m")
    assert messages == snapshot
    assert out is not messages
    assert out[-1] is not messages[-1]
    assert "cache_control" in out[-1]["content"][-1]


def test_string_content_converted_on_the_copy_only():
    messages = [{"role": "user", "content": "plain string"}]
    out = annotate_messages_cache(messages, "1h")
    assert messages[0]["content"] == "plain string"
    assert out[0]["content"][0]["text"] == "plain string"
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_degenerate_shapes_pass_through_unchanged():
    assert annotate_messages_cache([], "1h") == []
    empty_str = [{"role": "user", "content": ""}]
    assert annotate_messages_cache(empty_str, "1h") == empty_str
    weird = [{"role": "user", "content": [{"type": "text", "text": "x"}, "raw-string-block"]}]
    assert annotate_messages_cache(weird, "1h") == weird


def test_exactly_one_new_breakpoint():
    """Budget check: this helper adds exactly ONE cache_control across the
    whole request (tools + system own the other two of Anthropic's four)."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "a"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "y"}]},
    ]
    out = annotate_messages_cache(messages, "1h")
    count = sum(
        1
        for m in out
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(b, dict) and "cache_control" in b
    )
    assert count == 1
