"""Unit tests for server/chat/compaction.py pure helpers extracted during
the chat package split: estimate_message_size, microcompact_messages, and
_compact_messages_simple. The async compact_messages_with_claude path is not
covered here (it requires a live Bedrock client); these cover the
deterministic local-only logic the chat loop relies on for sizing and
microcompaction.
"""

from server.chat.compaction import (
    _compact_messages_simple,
    _is_clean_user_start,
    _safe_recent_split,
    ensure_valid_start,
    estimate_message_size,
    microcompact_messages,
    sanitize_tool_pairs,
)


def _tool_result_msg(tid="t1"):
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tid, "content": "x"}],
    }


def _tool_use_msg(tid="t1"):
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": tid, "name": "n", "input": {}}],
    }


def test_is_clean_user_start():
    assert _is_clean_user_start({"role": "user", "content": "hi"}) is True
    assert (
        _is_clean_user_start({"role": "user", "content": [{"type": "text", "text": "hi"}]}) is True
    )
    assert _is_clean_user_start(_tool_result_msg()) is False
    assert _is_clean_user_start({"role": "assistant", "content": "hi"}) is False


def test_ensure_valid_start_drops_orphan_tool_result():
    # A history that begins with a tool_result (its tool_use dropped) is trimmed
    # to the next clean user turn.
    msgs = [_tool_result_msg(), _tool_use_msg("t2"), {"role": "user", "content": "next"}]
    out = ensure_valid_start(msgs)
    assert out[0] == {"role": "user", "content": "next"}
    # An already-clean history is unchanged.
    clean = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert ensure_valid_start(clean) == clean


def test_safe_recent_split_never_starts_on_tool_result():
    # Alternating tool turns; the naive boundary would land on a tool_result.
    msgs = []
    for i in range(6):
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append(_tool_use_msg(f"t{i}"))
        msgs.append(_tool_result_msg(f"t{i}"))
    split = _safe_recent_split(msgs, keep_recent=8)
    assert _is_clean_user_start(msgs[split]) or split == 0


def test_compact_simple_never_starts_on_tool_result():
    # Build an over-long tool-heavy history and confirm the simple fallback
    # never returns a list whose head is an orphaned tool_result.
    msgs = []
    for i in range(10):
        msgs.append({"role": "user", "content": "q" * 3000})
        msgs.append(_tool_use_msg(f"t{i}"))
        msgs.append(_tool_result_msg(f"t{i}"))
    out = _compact_messages_simple(msgs, "model")
    assert out, "should not be empty"
    assert _is_clean_user_start(out[0])


def test_estimate_size_string_content():
    # len("hello") + 20 per-message overhead.
    assert estimate_message_size([{"role": "user", "content": "hello"}]) == 25


def test_estimate_size_list_content_sums_blocks():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "abc"},
                {"type": "tool_result", "content": "defg"},
            ],
        }
    ]
    # 3 (text) + 4 (content) + 20 overhead.
    assert estimate_message_size(msgs) == 27


def test_microcompact_noop_under_keep_recent():
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(4)]
    assert microcompact_messages(msgs, keep_recent=6) == msgs


def test_microcompact_preserves_message_count_and_recent_tail():
    # 12 messages, keep last 6 untouched. Older user tool_result blocks get
    # truncated but no message is ever dropped.
    msgs = []
    for _ in range(12):
        msgs.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "content": "X" * 5000}],
            }
        )
    out = microcompact_messages(msgs, keep_recent=6)
    assert len(out) == len(msgs)
    # Last 6 are kept intact (same object identity / content).
    assert out[-6:] == msgs[-6:]
    # At least one older tool_result was shrunk.
    first_block = out[0]["content"][0]["content"]
    assert len(first_block) < 5000


def test_microcompact_leaves_assistant_messages_intact():
    msgs = [{"role": "assistant", "content": [{"type": "text", "text": "T" * 9000}]}]
    msgs += [{"role": "user", "content": f"m{i}"} for i in range(8)]
    out = microcompact_messages(msgs, keep_recent=6)
    # The assistant message (non-user) is never rewritten by microcompact.
    assert out[0] == msgs[0]


def test_compact_simple_noop_under_four():
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert _compact_messages_simple(msgs, "model-x") == msgs


def test_compact_simple_truncates_old_long_string():
    old = {"role": "user", "content": "Q" * 5000}
    recent = [{"role": "user", "content": f"r{i}"} for i in range(8)]
    out = _compact_messages_simple([old] + recent, "model-x")
    assert len(out) == 9
    # Oldest long content was trimmed; recents preserved.
    assert out[0]["content"].endswith("... (trimmed)")
    assert len(out[0]["content"]) < 5000
    assert out[-8:] == recent


def test_sanitize_passes_well_formed_history_unchanged():
    msgs = [
        {"role": "user", "content": "hi"},
        _tool_use_msg("t1"),
        _tool_result_msg("t1"),
        {"role": "assistant", "content": "done"},
    ]
    out = sanitize_tool_pairs(msgs)
    assert out == msgs
    # Identity preserved for untouched messages (no needless copies).
    assert out[0] is msgs[0]
    assert out[1] is msgs[1]


def test_sanitize_drops_orphan_tool_result_mid_history():
    # The reported bug: an assistant turn whose tool_use was stripped leaves a
    # dangling tool_result. Bedrock rejects the whole request; sanitize must
    # drop the orphan and keep the surrounding structure intact.
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": [{"type": "text", "text": "partial reply"}]},
        _tool_result_msg("toolu_bdrk_orphan"),  # <- tool_use is gone
        {"role": "user", "content": "q2"},
    ]
    out = sanitize_tool_pairs(msgs)
    # Same count + roles (alternation preserved), no tool_result survives.
    assert [m["role"] for m in out] == ["user", "assistant", "user", "user"]
    blocks = [b for m in out if isinstance(m["content"], list) for b in m["content"]]
    assert all(b.get("type") != "tool_result" for b in blocks)
    # The emptied tool_result message is kept as a placeholder, not dropped.
    assert out[2]["content"] == [{"type": "text", "text": "(omitted)"}]


def test_sanitize_drops_orphan_tool_use_with_no_result():
    # A tool_use never answered by a tool_result in the next message is invalid
    # too — drop it, keep any sibling text.
    msgs = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "thinking"},
                {"type": "tool_use", "id": "t9", "name": "n", "input": {}},
            ],
        },
        {"role": "user", "content": "unrelated follow-up"},  # no tool_result
    ]
    out = sanitize_tool_pairs(msgs)
    assert out[1]["content"] == [{"type": "text", "text": "thinking"}]


def test_sanitize_requires_immediately_preceding_tool_use():
    # A tool_result whose tool_use is two messages back (an intervening
    # assistant message) is still invalid per Bedrock's adjacency rule.
    msgs = [
        _tool_use_msg("t1"),
        {"role": "assistant", "content": [{"type": "text", "text": "interjection"}]},
        _tool_result_msg("t1"),
    ]
    out = sanitize_tool_pairs(msgs)
    # t1's tool_use is now orphaned (next msg has no result) -> dropped.
    assert out[0]["content"] == [{"type": "text", "text": "(omitted)"}]
    # t1's tool_result's preceding message has no tool_use -> dropped.
    assert out[2]["content"] == [{"type": "text", "text": "(omitted)"}]


def test_sanitize_handles_empty_and_string_content():
    assert sanitize_tool_pairs([]) == []
    plain = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert sanitize_tool_pairs(plain) == plain
