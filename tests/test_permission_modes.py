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

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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


# ── category_modes ───────────────────────────────────────────────────────────


def _patch_permissions(monkeypatch, *, mode="default", rules=None, category_modes=None):
    """Like _patch_rules, but lets a test also control (or omit) category_modes."""
    data = {"mode": mode, "rules": rules if rules is not None else []}
    if category_modes is not None:
        data["category_modes"] = category_modes
    monkeypatch.setattr("server.security.permissions.load_permissions", lambda: data)


def test_category_mode_overrides_global_mode(monkeypatch):
    # Global mode is the maximally-permissive bypass, but "delete" has its own
    # stricter default — the override must win.
    _patch_permissions(monkeypatch, mode=MODE_BYPASS, category_modes={"delete": MODE_DEFAULT})
    decision = resolve_static_decision(
        "ws_delete_file",
        {"path": "foo.txt"},
        category="delete",
        session_approvals={},
        mode=MODE_BYPASS,
    )
    assert decision == "ask"


def test_category_mode_falls_back_to_global_when_unset(monkeypatch):
    # "cli" has an override but "write" doesn't -> write falls back to the
    # global mode passed in (bypass).
    _patch_permissions(monkeypatch, mode=MODE_BYPASS, category_modes={"cli": MODE_DEFAULT})
    decision = resolve_static_decision(
        "ws_write_file",
        {"path": "foo.txt"},
        category="write",
        session_approvals={},
        mode=MODE_BYPASS,
    )
    assert decision == "allow"


def test_github_destructive_always_asks_regardless_of_category_mode(monkeypatch):
    # The one invariant that must never break: even an explicit bypass
    # override for github-destructive itself can't loosen it.
    _patch_permissions(
        monkeypatch, mode=MODE_BYPASS, category_modes={"github-destructive": MODE_BYPASS}
    )
    decision = resolve_static_decision(
        "github_destructive",
        {"args": ["repo", "delete", "foo/bar"]},
        category="github-destructive",
        session_approvals={},
        mode=MODE_BYPASS,
    )
    assert decision == "ask"


def test_category_modes_missing_key_behaves_as_no_override(monkeypatch):
    # A legacy permissions.json (predating this feature) has no
    # "category_modes" key at all — must not crash, must behave as if no
    # category has an override.
    _patch_permissions(monkeypatch, mode=MODE_DEFAULT, category_modes=None)
    decision = resolve_static_decision(
        "ws_write_file",
        {"path": "foo.txt"},
        category="write",
        session_approvals={},
        mode=MODE_DEFAULT,
    )
    assert decision == "ask"


def test_load_permissions_legacy_file_has_no_category_modes_key(tmp_path, monkeypatch):
    """Integration check on load_permissions() itself (not resolve_static_decision):
    a real on-disk permissions.json written before this feature existed must
    load without crashing, filled in with an empty category_modes."""
    import json

    from server.security.permissions import load_permissions

    legacy_path = tmp_path / "permissions.json"
    legacy_path.write_text(json.dumps({"mode": "default", "rules": []}))
    monkeypatch.setattr("server.security.permissions.PERMISSIONS_PATH", str(legacy_path))

    data = load_permissions()
    assert data["category_modes"] == {}


# ── HTTP routes: justification + dry-run test endpoint ──────────────────────


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "server.security.permissions.PERMISSIONS_PATH", str(tmp_path / "permissions.json")
    )
    from server.security.permissions import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_rule_justification_round_trips_through_save_and_load(client):
    r = client.post(
        "/api/permissions/rules",
        json={
            "tool": "aws_cli",
            "pattern": "*",
            "action": "deny",
            "justification": "block prod-account CLI access from this session",
        },
    )
    assert r.status_code == 200
    added_rule = r.json()["rules"][-1]
    assert added_rule["justification"] == "block prod-account CLI access from this session"

    # Reload from disk (a fresh GET), not just the echoed response.
    reloaded = client.get("/api/permissions").json()
    assert reloaded["rules"][-1]["justification"] == (
        "block prod-account CLI access from this session"
    )


def test_rule_without_justification_omits_the_field(client):
    r = client.post(
        "/api/permissions/rules", json={"tool": "aws_cli", "pattern": "*", "action": "allow"}
    )
    assert "justification" not in r.json()["rules"][-1]


def test_dry_run_endpoint_reports_deny_for_matching_rule(client):
    client.post(
        "/api/permissions/rules",
        json={"tool": "aws_cli", "pattern": "*", "action": "deny"},
    )
    r = client.post(
        "/api/permissions/rules/test",
        json={"tool": "aws_cli", "input": {"command": "aws s3 rm s3://bucket --recursive"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "deny"
    assert body["matched_index"] == 0


def test_dry_run_endpoint_reports_no_match_as_none(client):
    r = client.post(
        "/api/permissions/rules/test",
        json={"tool": "ws_read_file", "input": {"path": "foo.txt"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] is None
    assert body["matched_index"] is None
    assert body["matched_rule"] is None


def test_dry_run_endpoint_tests_a_draft_rule_before_it_is_saved(client):
    # No persisted rules at all — the draft is passed inline via "rules" and
    # still reports a match, so the rule editor's Test button works before
    # the user has clicked Save.
    r = client.post(
        "/api/permissions/rules/test",
        json={
            "tool": "ws_write_file",
            "input": {"path": "secrets.env"},
            "rules": [{"tool": "ws_write_file", "pattern": "*.env", "action": "deny"}],
        },
    )
    body = r.json()
    assert body["decision"] == "deny"
    assert body["matched_rule"]["pattern"] == "*.env"
    # Nothing was actually persisted.
    assert client.get("/api/permissions").json()["rules"] == []


def test_dry_run_endpoint_requires_tool(client):
    r = client.post("/api/permissions/rules/test", json={"input": {}})
    assert r.status_code == 400
