"""Tests for the permission-mode precedence chain and custom rule evaluation.

check_permission() used to be the only code that read the (now-deleted)
WRITE_TOOLS set and branched on acceptEdits/bypassPermissions/dontAsk. Since
it was never called, those three modes and the custom Permission Rules
editor had zero effect on execution. resolve_static_decision() and
evaluate_rules() replace it, wired into the live [WS_APPROVAL] approval flow
(server.tool_executor.process_tool_results) via tool categories instead of
the old tool-name lists.
"""

import asyncio
from unittest.mock import AsyncMock

from server.approval.bootstrap import register_defaults
from server.security.permissions import (
    MODE_ACCEPT_EDITS,
    MODE_AUTO,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_DONT_ASK,
    evaluate_rules,
    is_auto_mode_tripped,
    record_classifier_verdict,
    reset_auto_mode_breaker,
    resolve_static_decision,
    resume_auto_mode,
)
from server.tool_executor import process_tool_results

register_defaults()


# ── evaluate_rules ──────────────────────────────────────────────────────────


def test_pattern_match():
    rules = [{"tool": "aws_cli", "pattern": "*", "action": "deny"}]
    assert evaluate_rules("aws_cli", {"command": "aws s3 ls"}, rules) == "deny"


def test_prefix_match():
    rules = [{"tool": "ws_run_command", "prefix": "git ", "action": "allow"}]
    assert evaluate_rules("ws_run_command", {"command": "git status"}, rules) == "allow"
    assert evaluate_rules("ws_run_command", {"command": "rm -rf /"}, rules) is None


def test_wildcard_tool_matches_anything():
    rules = [{"tool": "*", "pattern": "*", "action": "ask"}]
    assert evaluate_rules("literally_anything", {}, rules) == "ask"


def test_first_match_wins():
    rules = [
        {"tool": "aws_cli", "pattern": "*", "action": "deny"},
        {"tool": "aws_cli", "pattern": "*", "action": "allow"},
    ]
    assert evaluate_rules("aws_cli", {"command": "aws s3 ls"}, rules) == "deny"


def test_no_match_returns_none():
    rules = [{"tool": "git_push", "pattern": "*", "action": "deny"}]
    assert evaluate_rules("aws_cli", {"command": "aws s3 ls"}, rules) is None


# ── resolve_static_decision ─────────────────────────────────────────────────


def _patch_rules(monkeypatch, rules):
    monkeypatch.setattr(
        "server.security.permissions.load_permissions",
        lambda: {"mode": "default", "rules": rules},
    )


def test_bypass_overrides_an_explicit_deny_rule(monkeypatch):
    _patch_rules(monkeypatch, [{"tool": "aws_cli", "pattern": "*", "action": "deny"}])
    decision = resolve_static_decision(
        "aws_cli",
        {"command": "aws s3 rm s3://bucket --recursive"},
        category="cli",
        session_approvals={},
        mode=MODE_BYPASS,
    )
    assert decision == "allow"


def test_dont_ask_denies_when_nothing_else_matches(monkeypatch):
    _patch_rules(monkeypatch, [])
    decision = resolve_static_decision(
        "ws_write_file",
        {"path": "foo.txt"},
        category="write",
        session_approvals={},
        mode=MODE_DONT_ASK,
    )
    assert decision == "deny"


def test_accept_edits_allows_write_but_defers_delete(monkeypatch):
    _patch_rules(monkeypatch, [])
    write_decision = resolve_static_decision(
        "ws_write_file",
        {"path": "foo.txt"},
        category="write",
        session_approvals={},
        mode=MODE_ACCEPT_EDITS,
    )
    delete_decision = resolve_static_decision(
        "ws_delete_file",
        {"path": "foo.txt"},
        category="delete",
        session_approvals={},
        mode=MODE_ACCEPT_EDITS,
    )
    assert write_decision == "allow"
    assert delete_decision == "ask"


def test_custom_rule_beats_mode_default(monkeypatch):
    # dontAsk would otherwise deny every write; an explicit allow rule wins.
    _patch_rules(monkeypatch, [{"tool": "ws_write_file", "pattern": "*", "action": "allow"}])
    decision = resolve_static_decision(
        "ws_write_file",
        {"path": "foo.txt"},
        category="write",
        session_approvals={},
        mode=MODE_DONT_ASK,
    )
    assert decision == "allow"


def test_session_approval_beats_a_rule(monkeypatch):
    # Documented precedence: session approvals -> explicit rules -> mode defaults.
    _patch_rules(monkeypatch, [{"tool": "aws_cli", "pattern": "*", "action": "deny"}])
    decision = resolve_static_decision(
        "aws_cli",
        {"command": "aws s3 ls"},
        category="cli",
        session_approvals={"cli": "allow"},
        mode=MODE_DEFAULT,
    )
    assert decision == "allow"


def test_auto_mode_defers_when_nothing_resolved(monkeypatch):
    _patch_rules(monkeypatch, [])
    decision = resolve_static_decision(
        "ws_run_command",
        {"command": "npm install"},
        category="cli",
        session_approvals={},
        mode=MODE_AUTO,
    )
    assert decision is None


def test_default_mode_asks(monkeypatch):
    _patch_rules(monkeypatch, [])
    decision = resolve_static_decision(
        "ws_write_file",
        {"path": "foo.txt"},
        category="write",
        session_approvals={},
        mode=MODE_DEFAULT,
    )
    assert decision == "ask"


# ── process_tool_results: classifier fallback wiring ────────────────────────


class _State:
    """Mimics the StreamingToolExecutor state shape used by process_tool_results."""

    def __init__(self, tool_id, tool_name, output):
        self.tool_id = tool_id
        self.tool_name = tool_name
        self.output = output
        self.side_effects = []
        self.status = "pending"


def _budget_passthrough(_name, output):
    return output


def _write_approval_state():
    payload = '{"action": "ws_write_file", "path": "foo.txt", "content": "hi", "original": ""}'
    return _State(tool_id="tu_1", tool_name="ws_write_file", output=f"[WS_APPROVAL]{payload}")


def test_classifier_allow_skips_the_approval_banner(monkeypatch):
    _patch_rules(monkeypatch, [])
    monkeypatch.setattr(
        "server.tool_executor.classify_tool_call",
        AsyncMock(return_value={"decision": "allow", "reason": "safe"}),
    )
    monkeypatch.setattr(
        "server.tool_executor._execute_ws_approval_inline",
        AsyncMock(return_value="[OK] wrote foo.txt"),
    )
    tool_results, sse_events, has_pending_approval, has_user_question = asyncio.run(
        process_tool_results(
            [_write_approval_state()],
            budget_fn=_budget_passthrough,
            session_approvals={},
            config={},
            model_id="test-model",
            recent_messages=[],
            mode=MODE_AUTO,
        )
    )
    assert has_pending_approval is False
    assert not any('"approval_request":' in e for e in sse_events)
    assert tool_results[0]["content"] == "[OK] wrote foo.txt"


def test_classifier_confirm_still_shows_the_banner(monkeypatch):
    _patch_rules(monkeypatch, [])
    monkeypatch.setattr(
        "server.tool_executor.classify_tool_call",
        AsyncMock(return_value={"decision": "confirm", "reason": "needs a human"}),
    )
    monkeypatch.setattr(
        "server.tool_executor.explain_permission",
        AsyncMock(return_value=None),
    )
    tool_results, sse_events, has_pending_approval, has_user_question = asyncio.run(
        process_tool_results(
            [_write_approval_state()],
            budget_fn=_budget_passthrough,
            session_approvals={},
            config={},
            model_id="test-model",
            recent_messages=[],
            mode=MODE_AUTO,
        )
    )
    assert has_pending_approval is True
    assert any('"approval_request":' in e for e in sse_events)


def test_process_tool_results_passes_recent_messages_to_classifier(monkeypatch):
    # classify_tool_call used to see the tool call in total isolation; it now
    # receives the turn's recent (assistant) message slice unchanged, so it can
    # catch a multi-step pattern (e.g. a credentials read before this POST).
    _patch_rules(monkeypatch, [])
    classify_mock = AsyncMock(return_value={"decision": "allow", "reason": "ok"})
    monkeypatch.setattr("server.tool_executor.classify_tool_call", classify_mock)
    monkeypatch.setattr(
        "server.tool_executor._execute_ws_approval_inline",
        AsyncMock(return_value="[OK]"),
    )

    recent = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "ws_read_file", "input": {"path": "credentials.json"}}
            ],
        }
    ]
    asyncio.run(
        process_tool_results(
            [_write_approval_state()],
            budget_fn=_budget_passthrough,
            session_approvals={},
            config={},
            model_id="test-model",
            recent_messages=recent,
            mode=MODE_AUTO,
            session_id="breaker-context-passthrough",
        )
    )
    assert classify_mock.await_args.kwargs["recent_messages"] == recent


# ── auto-mode circuit breaker ────────────────────────────────────────────────
#
# The classifier can flip-flop over a long turn; record_classifier_verdict /
# is_auto_mode_tripped / resume_auto_mode (server.security.permissions) track
# its denials per session, scoped to a turn, and trip a breaker that forces
# "ask" instead of continuing to consult the classifier. Thresholds: 3
# "confirm" verdicts in a row, or 5 within the last 10 calls this turn.


def _run_write_approval(monkeypatch, session_id):
    return asyncio.run(
        process_tool_results(
            [_write_approval_state()],
            budget_fn=_budget_passthrough,
            session_approvals={},
            config={},
            model_id="test-model",
            recent_messages=[],
            mode=MODE_AUTO,
            session_id=session_id,
        )
    )


def test_breaker_trips_after_three_consecutive_denials(monkeypatch):
    _patch_rules(monkeypatch, [])
    session_id = "breaker-consecutive"
    reset_auto_mode_breaker(session_id)
    monkeypatch.setattr("server.tool_executor.explain_permission", AsyncMock(return_value=None))
    classify_mock = AsyncMock(return_value={"decision": "confirm", "reason": "no"})
    monkeypatch.setattr("server.tool_executor.classify_tool_call", classify_mock)

    sse_events = []
    for _ in range(3):
        _, sse_events, has_pending_approval, _ = _run_write_approval(monkeypatch, session_id)
        assert has_pending_approval is True

    assert is_auto_mode_tripped(session_id) is True
    # The breaker notification fires exactly once, right on the trip.
    assert any('"auto_mode_breaker":' in e for e in sse_events)
    assert classify_mock.call_count == 3

    # A 4th call must skip the classifier entirely — straight to "ask".
    _, sse_events, has_pending_approval, _ = _run_write_approval(monkeypatch, session_id)
    assert has_pending_approval is True
    assert classify_mock.call_count == 3
    assert not any('"auto_mode_breaker":' in e for e in sse_events)


def test_breaker_does_not_trip_on_occasional_denials(monkeypatch):
    _patch_rules(monkeypatch, [])
    session_id = "breaker-occasional"
    reset_auto_mode_breaker(session_id)
    monkeypatch.setattr("server.tool_executor.explain_permission", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "server.tool_executor._execute_ws_approval_inline",
        AsyncMock(return_value="[OK]"),
    )

    # 3 denials total, alternating with allows so consecutive never exceeds 1
    # and the 5-in-10 window never fills either — auto mode should keep
    # working normally the whole way through.
    decisions = ["allow", "confirm", "allow", "confirm", "allow", "confirm"]
    classify_mock = AsyncMock(side_effect=[{"decision": d, "reason": "x"} for d in decisions])
    monkeypatch.setattr("server.tool_executor.classify_tool_call", classify_mock)

    for expected in decisions:
        _, sse_events, has_pending_approval, _ = _run_write_approval(monkeypatch, session_id)
        assert has_pending_approval == (expected == "confirm")
        assert not any('"auto_mode_breaker":' in e for e in sse_events)

    assert is_auto_mode_tripped(session_id) is False
    assert classify_mock.call_count == len(decisions)


def test_breaker_trips_via_rolling_window_without_three_consecutive():
    # 5 denials in 9 calls, never more than one in a row — the consecutive
    # counter alone would never catch this; the window does.
    session_id = "breaker-window"
    reset_auto_mode_breaker(session_id)
    pattern = [False, True, False, True, False, True, False, True, False]

    state = None
    tripped_at = None
    for i, allowed in enumerate(pattern):
        state = record_classifier_verdict(session_id, allowed)
        if state["tripped"]:
            tripped_at = i
            break

    assert tripped_at == 8
    assert state["reason"] is not None
    assert "5" in state["reason"]


def test_resume_auto_mode_restores_classifier_for_rest_of_turn(monkeypatch):
    _patch_rules(monkeypatch, [])
    session_id = "breaker-resume"
    reset_auto_mode_breaker(session_id)
    monkeypatch.setattr("server.tool_executor.explain_permission", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "server.tool_executor._execute_ws_approval_inline",
        AsyncMock(return_value="[OK]"),
    )
    classify_mock = AsyncMock(return_value={"decision": "confirm", "reason": "no"})
    monkeypatch.setattr("server.tool_executor.classify_tool_call", classify_mock)

    for _ in range(3):
        _run_write_approval(monkeypatch, session_id)
    assert is_auto_mode_tripped(session_id) is True
    assert classify_mock.call_count == 3

    assert resume_auto_mode(session_id) is True
    assert is_auto_mode_tripped(session_id) is False

    # Subsequent tool calls in the same turn consult the classifier again.
    classify_mock.return_value = {"decision": "allow", "reason": "fine now"}
    tool_results, _, has_pending_approval, _ = _run_write_approval(monkeypatch, session_id)
    assert has_pending_approval is False
    assert classify_mock.call_count == 4
    assert tool_results[0]["content"] == "[OK]"


def test_resume_auto_mode_is_a_noop_when_nothing_tripped():
    session_id = "breaker-resume-noop"
    reset_auto_mode_breaker(session_id)
    assert resume_auto_mode(session_id) is False
    assert is_auto_mode_tripped(session_id) is False
