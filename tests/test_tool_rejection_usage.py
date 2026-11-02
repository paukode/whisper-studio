"""Rejected tool rounds retain real usage or explicitly identified estimates."""

from server.costs import tracker
from tests.test_engine_openai import _completed, _gpt_turn
from tests.test_tool_stream_rejection import (
    _anthropic_delta,
    _anthropic_events,
    _anthropic_start,
    _assert_rejected,
    _done,
    _start,
)


def test_openai_rejected_arguments_record_reported_usage_exactly_once(monkeypatch):
    recorded = []
    monkeypatch.setattr(tracker, "record_turn", lambda **values: recorded.append(values))
    lines, responses = _gpt_turn(
        monkeypatch,
        [[_start(), _done('{"path":'), _completed(40, 9)]],
    )
    assert len(responses.captured) == 1
    assert any('"error"' in line for line in lines)
    assert lines[-1] == "[DONE]"
    rows = [row for row in recorded if row["session_id"] == "golden-session"]
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 40
    assert rows[0]["output_tokens"] == 9
    assert rows[0]["estimated"] == ()


def test_anthropic_rejection_without_counts_retains_explicit_usage_estimates():
    adapter, events = _anthropic_events(
        [
            {"type": "message_start", "message": {}},
            _anthropic_start({}),
            _anthropic_delta("{"),
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ]
    )
    _assert_rejected(events)
    usage = adapter.inflight_usage()
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    assert usage.estimated == ("input", "output")
