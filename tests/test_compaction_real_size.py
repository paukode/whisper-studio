"""Compaction measures what the model is billed, clears old tool pictures,
writes a structured handover note, and does not end a turn that has room."""

import asyncio
import base64
import io
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from PIL import Image

import server.openai_bedrock.runtime  # noqa: F401
from server.chat import compaction, compaction_log, image_size, infra
from server.infrastructure import config, oneshot

SID = "real-size-session"


@pytest.fixture(autouse=True)
def models(monkeypatch):
    cfg = {
        "model_mode": "cloud",
        "chat_models": {"claude": "global.anthropic.claude-haiku-4-5", "gpt": "openai.gpt-5.5"},
        "chat_model_meta": {"gpt": {"provider": "openai_bedrock"}},
    }
    monkeypatch.setattr(config, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(infra, "_get_chat_models", lambda: cfg["chat_models"])
    monkeypatch.setattr(infra, "_get_chat_model_meta", lambda: cfg["chat_model_meta"])
    monkeypatch.setattr("server.local.runtime.is_local_model", lambda k: False)
    monkeypatch.setattr("server.openai_bedrock.runtime.is_openai_model", lambda k: k == "gpt")
    monkeypatch.setattr(compaction, "thresholds_for", lambda *a, **k: (10_000, 1_000_000))
    monkeypatch.setattr("server.infrastructure.feature_flags.is_enabled", lambda *_: True)
    compaction._summary_hold.clear()
    return cfg


def picture(width, height) -> str:
    """A noisy JPEG, so its data is far longer than what its pixels cost."""
    img = Image.effect_noise((width, height), 90).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def image_block(data):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def text_history():
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}: " + "x" * 3000}
        for i in range(14)
    ]


def screenshot_turn(data, pairs=10):
    messages = [{"role": "user", "content": [image_block(data), {"type": "text", "text": "go"}]}]
    for i in range(pairs):
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": str(i), "name": "shot", "input": {}}],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(i),
                        "content": [image_block(data), {"type": "text", "text": f"view {i}"}],
                    }
                ],
            }
        )
    return messages


def compact(messages, trigger="pressure", model_key="claude"):
    return asyncio.run(
        compaction.compact_messages_with_claude(
            messages, "unused-id", session_id=SID, model_key=model_key, trigger=trigger
        )
    )


def test_a_picture_costs_its_patches_for_each_provider():
    assert image_size.claude_image_tokens(1568, 724) == 56 * 26
    assert image_size.claude_image_tokens(1280, 800) == 46 * 29
    assert image_size.gpt_image_tokens(1280, 800) == 1200  # 40 x 25 patches, times 1.2
    # A picture over Claude's limits is scaled down before it is counted.
    assert image_size.claude_image_tokens(8000, 6000) <= image_size.CLAUDE_MAX_TOKENS
    assert image_size.claude_image_tokens(8000, 6000) > image_size.claude_image_tokens(1280, 800)


def test_the_estimate_counts_a_picture_by_pixels_not_by_data_length():
    data = picture(1280, 800)
    message = [{"role": "user", "content": [image_block(data)]}]
    claude = compaction.estimate_message_size(message, "claude")
    gpt = compaction.estimate_message_size(message, "gpt")
    assert claude == image_size.claude_image_tokens(1280, 800) * 4 + 20
    assert gpt == image_size.gpt_image_tokens(1280, 800) * 4 + 20
    assert claude < len(data) / 10
    # With no model named, the Claude rule applies.
    assert compaction.estimate_message_size(message) == claude


def test_unreadable_picture_data_counts_at_its_length():
    message = [{"role": "user", "content": [image_block("not-a-picture" * 50)]}]
    assert compaction.estimate_message_size(message) == len("not-a-picture" * 50) + 20


def test_clearing_removes_old_tool_pictures_and_keeps_recent_and_attached_ones():
    data = picture(320, 200)
    messages = screenshot_turn(data)
    out = compaction.microcompact_messages(messages, keep_recent=compaction.KEEP_RECENT)
    assert out[0] == messages[0]  # the user's own picture stays
    assert out[-compaction.KEEP_RECENT :] == messages[-compaction.KEEP_RECENT :]
    old_results = [
        b for m in out[1 : -compaction.KEEP_RECENT] if m["role"] == "user" for b in m["content"]
    ]
    assert old_results
    for block in old_results:
        kinds = [part["type"] for part in block["content"]]
        assert "image" not in kinds
        assert block["content"][0]["text"] == compaction.PICTURE_CLEARED
    assert compaction.sanitize_tool_pairs(out) == out
    assert compaction.estimate_message_size(out) < compaction.estimate_message_size(messages)


def test_a_failed_summary_with_room_keeps_the_turn_alive_and_records_why(monkeypatch):
    call = Mock(side_effect=RuntimeError("cloud one-shot completion was incomplete or refused"))
    monkeypatch.setattr(oneshot, "one_shot", call)
    ends = []
    monkeypatch.setattr(compaction_log, "record_end", lambda sid, **kw: ends.append(kw))
    messages = text_history()
    original = deepcopy(messages)
    out = compact(messages)
    assert out == original and messages == original
    assert call.call_count == 1
    assert ends[-1]["outcome"] == "summary-failed"
    assert "incomplete or refused" in ends[-1]["detail"]


def test_a_failed_summary_is_not_tried_again_until_the_history_grows(monkeypatch):
    call = Mock(side_effect=RuntimeError("no"))
    monkeypatch.setattr(oneshot, "one_shot", call)
    messages = text_history()
    compact(messages)
    compact(messages)
    assert call.call_count == 1
    grown = [*messages, {"role": "assistant", "content": "y" * 3000}] + [
        {"role": "user", "content": "z" * 3000}
    ]
    compact(grown)
    assert call.call_count == 2


def test_a_real_overflow_with_a_failed_summary_still_stops(monkeypatch):
    monkeypatch.setattr(oneshot, "one_shot", Mock(side_effect=RuntimeError("no")))
    messages = text_history()
    with pytest.raises(compaction.CompactionError) as caught:
        compact(messages, "context-overflow")
    assert "RuntimeError" in caught.value.reason
    # The reason is for the log; the user's message does not carry it.
    assert "RuntimeError" not in str(caught.value)


def test_a_cut_off_summary_is_used_as_far_as_it_got(monkeypatch):
    cut = oneshot.OneShotIncomplete("incomplete", "1. Context: the box is 55 mm high")
    monkeypatch.setattr(oneshot, "one_shot", Mock(side_effect=cut))
    out = compact(text_history())
    assert out[0]["content"].startswith(compaction.SUMMARY_HEADER)
    assert "the box is 55 mm high" in out[0]["content"]
    assert compaction.SUMMARY_CUT_OFF in out[0]["content"]


def test_a_cut_off_summary_with_no_text_is_a_failure(monkeypatch):
    monkeypatch.setattr(
        oneshot, "one_shot", Mock(side_effect=oneshot.OneShotIncomplete("incomplete", "  "))
    )
    with pytest.raises(compaction.CompactionError):
        compact(text_history(), "context-overflow")


def test_the_summary_asks_for_the_handover_sections_with_room_to_write_them(monkeypatch):
    call = Mock(return_value="1. Context: short")
    monkeypatch.setattr(oneshot, "one_shot", call)
    compact(text_history())
    prompt = call.call_args.args[1]
    for section in ("Context", "Decisions", "Outcomes", "Assumptions", "Pending", "Current work"):
        assert section in prompt
    assert call.call_args.kwargs["max_tokens"] == compaction.SUMMARY_MAX_TOKENS


def test_the_summary_input_leaves_out_thinking_signatures():
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "the lid needs four screws",
                    "signature": "S" * 900,
                },
                {"type": "redacted_thinking", "data": "R" * 900},
            ],
        }
    ]
    serialized = compaction._serialize_history(messages)
    assert "the lid needs four screws" in serialized
    assert "SSSS" not in serialized and "RRRR" not in serialized


def test_claude_one_shot_at_its_output_cap_hands_back_its_text(monkeypatch):
    payload = {
        "stop_reason": "max_tokens",
        "content": [{"type": "text", "text": "partial note"}],
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    client = Mock()
    client.invoke_model.return_value = {"body": io.BytesIO(json.dumps(payload).encode())}
    monkeypatch.setattr("server.costs.calls.record_invoke", Mock())
    with pytest.raises(oneshot.OneShotIncomplete) as caught:
        oneshot.one_shot(
            "sys",
            "usr",
            max_tokens=100,
            engine="cloud",
            cloud_model_key="claude",
            source="compaction",
            session_id="sid",
            get_bedrock_client=lambda: client,
        )
    assert caught.value.text == "partial note"


def test_token_pressure_goes_to_the_summary_when_the_estimate_is_under_its_trigger(monkeypatch):
    monkeypatch.setattr(compaction, "thresholds_for", lambda *a, **k: (900_000, 1_000_000))
    call = Mock(return_value="1. Context: short")
    monkeypatch.setattr(oneshot, "one_shot", call)
    messages = text_history()
    messages[0]["content"] = [{"type": "tool_result", "tool_use_id": "old", "content": "x" * 9000}]
    # By the estimate, clearing the old result is enough: no summary.
    assert compact(messages) != messages
    assert call.call_count == 0
    out = compact(messages, "token-pressure")
    assert call.call_count == 1
    assert out[0]["content"].startswith(compaction.SUMMARY_HEADER)


def test_too_many_tool_pictures_are_cleared_in_one_large_step():
    data = picture(64, 64)
    under = screenshot_turn(data, pairs=compaction.MAX_TOOL_PICTURES)
    assert compaction.limit_tool_pictures(under) is under
    over = screenshot_turn(data, pairs=compaction.MAX_TOOL_PICTURES + 1)
    original = deepcopy(over)
    out = compaction.limit_tool_pictures(over)
    assert over == original
    left = compaction._tool_picture_slots(out)
    assert len(left) == compaction.KEEP_TOOL_PICTURES
    # The newest pictures stay, and so does the one the user attached.
    assert out[-1] == over[-1] and out[0] == over[0]
    assert compaction.sanitize_tool_pairs(out) == out
    # The next rounds do not change the history again until the limit.
    assert compaction.limit_tool_pictures(out) is out


def test_a_held_summary_leaves_the_history_untouched_in_later_rounds(monkeypatch):
    monkeypatch.setattr(oneshot, "one_shot", Mock(side_effect=RuntimeError("no")))
    starts = []
    monkeypatch.setattr(compaction_log, "record_start", lambda sid, **kw: starts.append(kw))
    messages = text_history()
    messages[1]["content"] = [{"type": "tool_result", "tool_use_id": "t", "content": "x" * 9000}]
    first = compact(messages)
    assert len(starts) == 1
    # The same history again: nothing is cleared a second time, nothing logged.
    again = compact(first)
    assert again is first and len(starts) == 1
    # A real overflow is never held.
    with pytest.raises(compaction.CompactionError):
        compact(first, "context-overflow")
