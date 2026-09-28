"""An approval card for a hard floor says so.

The card's "Yes, all <category>" and "Block <category>" remember a choice
for the session, and the server applies that memory (session_approvals)
only after its hard floors: a destructive GitHub call, an rm-class command,
a sandbox escalation, an MCP server marked approve. For a floor request the
two buttons would be silent no-ops, so the approval_request frame carries
``always_asks`` and the card drops them. The flag and the decision come
from the same predicate, so they cannot disagree.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from server.approval.bootstrap import register_defaults
from server.security.permissions import (
    MODE_DEFAULT,
    approval_floor,
    resolve_static_decision,
)
from server.tool_executor import process_tool_results

register_defaults()


@pytest.fixture(autouse=True)
def _no_rules(monkeypatch):
    monkeypatch.setattr(
        "server.security.permissions.load_permissions",
        lambda: {"mode": "default", "rules": []},
    )
    monkeypatch.setattr("server.tool_executor.explain_permission", AsyncMock(return_value=None))


class _State:
    def __init__(self, tool_id, tool_name, payload):
        self.tool_id = tool_id
        self.tool_name = tool_name
        self.output = f"[WS_APPROVAL]{json.dumps(payload)}"
        self.side_effects = []
        self.status = "pending"


def _request(tool_name, payload, session_approvals):
    """The approval_request frame process_tool_results emits, or None."""
    _results, sse_events, has_pending, _q = asyncio.run(
        process_tool_results(
            [_State("tu_1", tool_name, payload)],
            budget_fn=lambda _n, out: out,
            session_approvals=session_approvals,
            config={},
            model_id="",
            recent_messages=[],
            mode=MODE_DEFAULT,
        )
    )
    for raw in sse_events:
        event = json.loads(raw)
        if "approval_request" in event:
            assert has_pending is True
            return event["approval_request"]
    return None


def test_a_floor_is_named_and_an_ordinary_call_has_none():
    assert approval_floor("ws_run_command", {"command": "rm -rf build"}, "cli") == "rm"
    assert approval_floor("github_destructive", {}, "github-destructive") == "github-destructive"
    assert (
        approval_floor(
            "ws_run_command",
            {"command": "make", "sandbox_permissions": "danger-full-access"},
            "cli",
        )
        == "sandbox-escalation"
    )
    assert approval_floor("ws_run_command", {"command": "npm test"}, "cli") is None


def test_every_floor_asks_whatever_the_session_remembers():
    for remembered in ("allow", "deny"):
        decision = resolve_static_decision(
            "ws_run_command",
            {"command": "rm -rf build"},
            category="cli",
            session_approvals={"cli": remembered},
            mode=MODE_DEFAULT,
        )
        assert decision == "ask"


def test_an_rm_card_says_it_always_asks_even_under_yes_all():
    req = _request(
        "ws_run_command",
        {"action": "ws_run_command", "command": "rm -rf build"},
        {"cli": "allow"},
    )
    assert req is not None
    assert req["category"] == "cli"
    assert req["always_asks"] is True


def test_an_ordinary_card_offers_the_session_choice():
    req = _request(
        "ws_run_command",
        {"action": "ws_run_command", "command": "npm test"},
        {},
    )
    assert req is not None
    assert req["always_asks"] is False
