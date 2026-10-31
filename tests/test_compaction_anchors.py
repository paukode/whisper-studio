"""server.chat.compaction_anchors: the deterministic companions of a compaction
summary (verbatim user messages, the regex anchor index, the session_search
recovery pointer) and their attachment to the summary message."""

from __future__ import annotations

import asyncio

from server.chat import compaction
from server.chat.compaction_anchors import (
    extract_anchors,
    recovery_pointer,
    render_summary_extras,
    verbatim_user_messages,
)


def _tool_result(text):
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t", "content": text}],
    }


def test_extract_anchors_finds_paths_prs_shas_urls_and_error_lines():
    msgs = [
        {"role": "user", "content": "Look at /Users/me/repo/server/chat/runner.py and PR #144."},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Commit 5aadb60 is merged. See https://example.com/x?y=1."}
            ],
        },
        _tool_result("Traceback (most recent call last):\n  File x\nValueError: bad thing"),
    ]
    anchors = extract_anchors(msgs)
    assert "/Users/me/repo/server/chat/runner.py" in anchors
    assert "#144" in anchors
    assert "5aadb60" in anchors
    assert "https://example.com/x?y=1" in anchors
    assert any(a.startswith("Traceback") for a in anchors)
    assert any(a.startswith("ValueError") for a in anchors)


def test_extract_anchors_dedupes_and_respects_caps():
    msgs = [{"role": "user", "content": " ".join(f"/tmp/file{i}/a.py" for i in range(100))}] * 3
    anchors = extract_anchors(msgs, max_items=10)
    assert len(anchors) == 10 and len(set(anchors)) == 10


def test_verbatim_user_messages_newest_first_skipping_tool_results_and_synthetic_rows():
    msgs = [
        {"role": "user", "content": "first instruction"},
        {"role": "assistant", "content": "ok"},
        _tool_result("output"),
        {"role": "user", "content": "[completion gate] not yet"},
        {"role": "user", "content": [{"type": "text", "text": "second instruction"}]},
        {"role": "user", "content": "<system-reminder>x</system-reminder>"},
    ]
    assert verbatim_user_messages(msgs) == ["second instruction", "first instruction"]


def test_verbatim_user_messages_honours_budget_and_per_message_cap():
    msgs = [{"role": "user", "content": "a" * 1000}, {"role": "user", "content": "b" * 1000}]
    out = verbatim_user_messages(msgs, budget_chars=900, per_message_chars=500)
    assert len(out) == 1 and out[0].startswith("b" * 500) and out[0].endswith("...")


def test_render_summary_extras_carries_pointer_only_with_a_session_id():
    msgs = [{"role": "user", "content": "keep /a/b/c.py"}]
    with_sid = render_summary_extras(msgs, "sess-1")
    assert "session_search" in with_sid and 'session_id="sess-1"' in with_sid
    assert "keep /a/b/c.py" in with_sid and "/a/b/c.py" in with_sid
    assert "session_search" not in render_summary_extras(msgs, "")
    assert recovery_pointer("x").startswith("Recovery:")


def test_current_summary_appends_users_and_pointer_without_session_cache(monkeypatch):
    from unittest.mock import Mock

    from server.memory import session_memory

    cache = Mock(side_effect=AssertionError("stale cache must not be read or written"))
    monkeypatch.setattr(session_memory, "load_session_memory", cache)
    monkeypatch.setattr(session_memory, "get_session_memory_path", cache)
    monkeypatch.setattr(compaction, "thresholds_for", lambda *a, **k: (100_000, 120_000))
    monkeypatch.setattr(
        "server.infrastructure.config.load_config",
        lambda: {"chat_models": {"claude": "global.anthropic.claude-haiku-4-5"}},
    )
    monkeypatch.setattr("server.local.runtime.is_local_model", lambda k: False)
    monkeypatch.setattr("server.infrastructure.oneshot.one_shot", lambda *a, **k: "Current summary")
    monkeypatch.setattr(
        "server.infrastructure.feature_flags.is_enabled", lambda name: name == "session_search"
    )
    msgs = []
    for i in range(6):
        msgs.append({"role": "user", "content": f"user instruction number {i}"})
        msgs.append({"role": "assistant", "content": f"reply {i} " + "x" * 2000})
    out, outcome = asyncio.run(compaction._compact_strategies(msgs, "model-id", "sess-9", "claude"))
    assert outcome == "summary"
    summary = out[0]["content"]
    assert "Current summary" in summary
    assert "user instruction number 1" in summary
    assert 'session_id="sess-9"' in summary
    assert out[1:] == msgs[4:]
    cache.assert_not_called()


def test_summary_model_key_follows_auxiliary_compaction_setting(monkeypatch):
    cfg = {
        "chat_models": {
            "opus5.0": "global.anthropic.claude-opus-5",
            "haiku": "global.anthropic.claude-haiku-4-5",
        }
    }
    monkeypatch.setattr("server.infrastructure.config.load_config", lambda: cfg)
    assert compaction._summary_model_key("opus5.0") == "opus5.0"
    cfg["auxiliary_models"] = {"compaction": "haiku"}
    assert compaction._summary_model_key("opus5.0") == "haiku"
    cfg["auxiliary_models"] = {"compaction": "main"}
    assert compaction._summary_model_key("opus5.0") == "opus5.0"
