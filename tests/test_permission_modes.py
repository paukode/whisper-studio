"""Tests for the permission-mode precedence chain.

The permission mode is the only permission setting a person changes.
resolve_static_decision() applies it, wired into the live [WS_APPROVAL]
approval flow (server.tool_executor.process_tool_results) via tool categories.
"""

import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.approval.bootstrap import register_defaults
from server.approval.markers import approval_request
from server.approval.spec import ApprovalOutcome
from server.security.permissions import (
    MODE_ACCEPT_EDITS,
    MODE_AUTO,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_DONT_ASK,
    is_auto_mode_tripped,
    record_classifier_verdict,
    reset_auto_mode_breaker,
    resolve_static_decision,
    resume_auto_mode,
)
from server.tool_executor import process_tool_results

register_defaults()


# ── resolve_static_decision ─────────────────────────────────────────────────


def _patch_mode(monkeypatch, mode="default"):
    monkeypatch.setattr("server.security.permissions.load_permissions", lambda: {"mode": mode})


def test_bypass_allows_a_destructive_cloud_command(monkeypatch):
    _patch_mode(monkeypatch)
    decision = resolve_static_decision(
        "aws_cli",
        {"command": "aws s3 rm s3://bucket --recursive"},
        category="cli",
        session_approvals={},
        mode=MODE_BYPASS,
    )
    assert decision == "allow"


def test_dont_ask_denies_when_nothing_else_matches(monkeypatch):
    _patch_mode(monkeypatch)
    decision = resolve_static_decision(
        "ws_write_file",
        {"path": "foo.txt"},
        category="write",
        session_approvals={},
        mode=MODE_DONT_ASK,
    )
    assert decision == "deny"


def test_accept_edits_allows_write_but_defers_delete(monkeypatch):
    _patch_mode(monkeypatch)
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


def test_a_session_yes_beats_the_mode_default(monkeypatch):
    # Documented precedence: session approvals -> mode defaults.
    _patch_mode(monkeypatch)
    decision = resolve_static_decision(
        "aws_cli",
        {"command": "aws s3 ls"},
        category="cli",
        session_approvals={"cli": "allow"},
        mode=MODE_DEFAULT,
    )
    assert decision == "allow"


def test_auto_mode_defers_when_nothing_resolved(monkeypatch):
    _patch_mode(monkeypatch)
    decision = resolve_static_decision(
        "ws_run_command",
        {"command": "npm install"},
        category="cli",
        session_approvals={},
        mode=MODE_AUTO,
    )
    assert decision is None


def test_default_mode_asks(monkeypatch):
    _patch_mode(monkeypatch)
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
    return _State(tool_id="tu_1", tool_name="ws_write_file", output=approval_request(payload))


def test_classifier_allow_skips_the_approval_banner(monkeypatch):
    _patch_mode(monkeypatch)
    monkeypatch.setattr(
        "server.tool_approvals.classify_tool_call",
        AsyncMock(return_value={"decision": "allow", "reason": "safe"}),
    )
    monkeypatch.setattr(
        "server.tool_approvals._execute_ws_approval_inline",
        AsyncMock(return_value=ApprovalOutcome(ok=True, output="[OK] wrote foo.txt")),
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
    _patch_mode(monkeypatch)
    monkeypatch.setattr(
        "server.tool_approvals.classify_tool_call",
        AsyncMock(return_value={"decision": "confirm", "reason": "needs a human"}),
    )
    monkeypatch.setattr(
        "server.tool_approvals.explain_permission",
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


def test_classifier_and_explainer_bill_the_chat_session_not_the_turn_scope(monkeypatch):
    # A scoped headless turn runs under the scope "exec:wake:..." while its
    # rounds are logged under the chat session. The side calls must land on
    # the chat session (its budget and readout), the breaker on the scope.
    _patch_mode(monkeypatch)
    classify = AsyncMock(return_value={"decision": "confirm", "reason": "needs a human"})
    explain = AsyncMock(return_value=None)
    breaker_keys = []

    def record_verdict(key, *, allowed):
        breaker_keys.append(key)
        return {"tripped": False}

    monkeypatch.setattr("server.tool_approvals.classify_tool_call", classify)
    monkeypatch.setattr("server.tool_approvals.explain_permission", explain)
    monkeypatch.setattr("server.security.permissions.record_classifier_verdict", record_verdict)
    asyncio.run(
        process_tool_results(
            [_write_approval_state()],
            budget_fn=_budget_passthrough,
            session_approvals={},
            config={"permission_explainer_enabled": True},
            model_id="test-model",
            recent_messages=[],
            mode=MODE_AUTO,
            session_id="exec:wake:chat1",
            cost_session_id="chat1",
        )
    )
    assert classify.call_args.kwargs["session_id"] == "chat1"
    assert explain.call_args.kwargs["session_id"] == "chat1"
    assert breaker_keys == ["exec:wake:chat1"]


# ── permissions.json ─────────────────────────────────────────────────────────


def test_github_destructive_asks_even_in_bypass():
    # The one invariant that must never break: bypass cannot loosen it.
    decision = resolve_static_decision(
        "github_destructive",
        {"args": ["repo", "delete", "foo/bar"]},
        category="github-destructive",
        session_approvals={},
        mode=MODE_BYPASS,
    )
    assert decision == "ask"


def test_github_destructive_is_denied_in_dont_ask():
    decision = resolve_static_decision(
        "github_destructive",
        {"args": ["repo", "delete", "foo/bar"]},
        category="github-destructive",
        session_approvals={},
        mode=MODE_DONT_ASK,
    )
    assert decision == "deny"


def test_an_older_file_loads_only_its_mode_and_a_save_drops_the_rest(tmp_path, monkeypatch):
    """A permissions.json written by an older version can hold rules, category
    rows and a network policy. Only the mode is read, and the next save of the
    mode writes the mode alone."""
    import json

    from server.security.permissions import load_permissions, save_mode

    path = tmp_path / "permissions.json"
    path.write_text(
        json.dumps(
            {
                "mode": "acceptEdits",
                "rules": [{"tool": "*", "action": "allow"}],
                "category_modes": {"cli": "bypassPermissions"},
                "network_policy": {"tier": "curated"},
            }
        )
    )
    monkeypatch.setattr("server.security.permissions.PERMISSIONS_PATH", str(path))
    assert load_permissions() == {"mode": "acceptEdits"}
    save_mode("plan")
    assert json.loads(path.read_text()) == {"mode": "plan"}


def test_an_unknown_mode_reads_as_the_default(tmp_path, monkeypatch):
    from server.security.permissions import load_permissions

    path = tmp_path / "permissions.json"
    path.write_text('{"mode": "everythingGoes"}')
    monkeypatch.setattr("server.security.permissions.PERMISSIONS_PATH", str(path))
    assert load_permissions() == {"mode": MODE_DEFAULT}


def test_a_corrupt_permissions_file_raises_and_is_not_overwritten(tmp_path, monkeypatch):
    """A corrupt file must not read as the default mode, and nothing may save
    the default over it."""
    from server.security.permissions import PermissionsFileError, load_permissions

    path = tmp_path / "permissions.json"
    path.write_text('{"mode": "bypassPermissions",}')
    monkeypatch.setattr("server.security.permissions.PERMISSIONS_PATH", str(path))
    with pytest.raises(PermissionsFileError):
        load_permissions()
    assert "bypassPermissions" in path.read_text()


def test_a_missing_permissions_file_reads_as_the_defaults(tmp_path, monkeypatch):
    from server.security.permissions import load_permissions

    monkeypatch.setattr(
        "server.security.permissions.PERMISSIONS_PATH", str(tmp_path / "permissions.json")
    )
    assert load_permissions() == {"mode": MODE_DEFAULT}


# ── HTTP routes: the mode is the only setting ───────────────────────────────


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "server.security.permissions.PERMISSIONS_PATH", str(tmp_path / "permissions.json")
    )
    from server.security.permissions import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_the_mode_round_trips_through_the_api(client):
    assert client.get("/api/permissions").json() == {"mode": MODE_DEFAULT}
    r = client.put("/api/permissions/mode", json={"mode": MODE_ACCEPT_EDITS})
    assert r.json() == {"mode": MODE_ACCEPT_EDITS}
    assert client.get("/api/permissions").json() == {"mode": MODE_ACCEPT_EDITS}


def test_an_unknown_mode_is_refused(client):
    r = client.put("/api/permissions/mode", json={"mode": "everythingGoes"})
    assert r.status_code == 400
    assert client.get("/api/permissions").json() == {"mode": MODE_DEFAULT}


def test_nothing_but_the_mode_can_be_changed_over_the_api(client):
    # No rules, category rows, network policy or folder grants to edit.
    refused = [
        client.put("/api/permissions", json={"category_modes": {"cli": MODE_BYPASS}}),
        client.post("/api/permissions/rules", json={"tool": "*", "action": "allow"}),
        client.post("/api/permissions/rules/test", json={"tool": "aws_cli"}),
        client.delete("/api/permissions/rules/0"),
        client.get("/api/permissions/folder-grants"),
        client.delete("/api/permissions/folder-grants?path=/tmp"),
    ]
    assert all(r.status_code in (404, 405) for r in refused)
    assert client.get("/api/permissions").json() == {"mode": MODE_DEFAULT}


def test_process_tool_results_passes_recent_messages_to_classifier(monkeypatch):
    # classify_tool_call used to see the tool call in total isolation; it now
    # receives the turn's recent (assistant) message slice unchanged, so it can
    # catch a multi-step pattern (e.g. a credentials read before this POST).
    _patch_mode(monkeypatch)
    classify_mock = AsyncMock(return_value={"decision": "allow", "reason": "ok"})
    monkeypatch.setattr("server.tool_approvals.classify_tool_call", classify_mock)
    monkeypatch.setattr(
        "server.tool_approvals._execute_ws_approval_inline",
        AsyncMock(return_value=ApprovalOutcome(ok=True, output="[OK]")),
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
    _patch_mode(monkeypatch)
    session_id = "breaker-consecutive"
    reset_auto_mode_breaker(session_id)
    monkeypatch.setattr("server.tool_approvals.explain_permission", AsyncMock(return_value=None))
    classify_mock = AsyncMock(return_value={"decision": "confirm", "reason": "no"})
    monkeypatch.setattr("server.tool_approvals.classify_tool_call", classify_mock)

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
    _patch_mode(monkeypatch)
    session_id = "breaker-occasional"
    reset_auto_mode_breaker(session_id)
    monkeypatch.setattr("server.tool_approvals.explain_permission", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "server.tool_approvals._execute_ws_approval_inline",
        AsyncMock(return_value=ApprovalOutcome(ok=True, output="[OK]")),
    )

    # 3 denials total, alternating with allows so consecutive never exceeds 1
    # and the 5-in-10 window never fills either — auto mode should keep
    # working normally the whole way through.
    decisions = ["allow", "confirm", "allow", "confirm", "allow", "confirm"]
    classify_mock = AsyncMock(side_effect=[{"decision": d, "reason": "x"} for d in decisions])
    monkeypatch.setattr("server.tool_approvals.classify_tool_call", classify_mock)

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
    _patch_mode(monkeypatch)
    session_id = "breaker-resume"
    reset_auto_mode_breaker(session_id)
    monkeypatch.setattr("server.tool_approvals.explain_permission", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "server.tool_approvals._execute_ws_approval_inline",
        AsyncMock(return_value=ApprovalOutcome(ok=True, output="[OK]")),
    )
    classify_mock = AsyncMock(return_value={"decision": "confirm", "reason": "no"})
    monkeypatch.setattr("server.tool_approvals.classify_tool_call", classify_mock)

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


# ── bypassPermissions has no workspace boundary ─────────────────────────────
# The user's decision of 2026-11-02 replaces the rule of 2026-10-18 ("a change
# outside the workspace asks even in bypass"): Bypass works like Claude Code's
# bypass, so a save to a path the user named and opening a folder outside the
# workspace run with no card.


def test_bypass_allows_a_save_outside_the_workspace(monkeypatch, tmp_path):
    _patch_mode(monkeypatch)
    decision = resolve_static_decision(
        "save_file",
        {"action": "save_to_path", "path": str(tmp_path / "Documents" / "notes.txt")},
        category="save",
        session_approvals={},
        mode=MODE_BYPASS,
    )
    assert decision == "allow"


def test_bypass_allows_reaching_into_a_new_folder(monkeypatch, tmp_path):
    _patch_mode(monkeypatch)
    decision = resolve_static_decision(
        "ws_open_folder",
        {"action": "folder_access", "path": str(tmp_path / "project2")},
        category="folder-access",
        session_approvals={},
        mode=MODE_BYPASS,
    )
    assert decision == "allow"


def test_an_outside_save_and_folder_reach_still_ask_outside_bypass(monkeypatch, tmp_path):
    _patch_mode(monkeypatch)
    for mode in (MODE_DEFAULT, MODE_ACCEPT_EDITS):
        assert (
            resolve_static_decision(
                "save_file",
                {"action": "save_to_path", "path": str(tmp_path / "notes.txt")},
                "save",
                {},
                mode,
            )
            == "ask"
        )
        assert (
            resolve_static_decision(
                "ws_open_folder",
                {"action": "folder_access", "path": str(tmp_path / "project2")},
                "folder-access",
                {},
                mode,
            )
            == "ask"
        )


def test_bypass_allows_a_save_under_the_apps_own_folder(monkeypatch):
    from server.infrastructure import paths

    _patch_mode(monkeypatch)
    rule = os.path.join(paths.user_root(), "rules", "global", "rules.md")
    decision = resolve_static_decision(
        "save_file",
        {"action": "save_to_path", "path": rule},
        category="save",
        session_approvals={},
        mode=MODE_BYPASS,
    )
    assert decision == "allow"


def test_a_session_yes_covers_an_outside_save(monkeypatch, tmp_path):
    path = str(tmp_path / "Documents" / "notes.txt")
    payload = {"action": "save_to_path", "path": path}
    _patch_mode(monkeypatch)
    assert (
        resolve_static_decision("save_file", payload, "save", {"save": "allow"}, MODE_DEFAULT)
        == "allow"
    )


def test_bypass_stays_absolute_inside_the_workspace(monkeypatch, tmp_path):
    _patch_mode(monkeypatch)
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(tmp_path))
    for tool, payload, category in (
        ("ws_write_file", {"action": "write", "path": "src/app.py"}, "write"),
        ("ws_delete_file", {"action": "delete", "path": "src/app.py"}, "delete"),
        ("ws_run_command", {"action": "command", "command": "rm -rf dist"}, "cli"),
    ):
        assert resolve_static_decision(tool, payload, category, {}, MODE_BYPASS) == "allow"
