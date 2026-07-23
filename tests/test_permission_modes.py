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
    resolve_static_decision,
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
            config={"auto_mode_enabled": True},
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
            config={"auto_mode_enabled": True},
            model_id="test-model",
            recent_messages=[],
            mode=MODE_AUTO,
        )
    )
    assert has_pending_approval is True
    assert any('"approval_request":' in e for e in sse_events)
