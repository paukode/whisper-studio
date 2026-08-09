"""MCP elicitation — a server asking the human for input mid tool-call.

`_build_elicitation_callback` is what `_serve()` wires into `ClientSession`
(see server/mcp.py). These tests exercise that callback directly (the same
function `_serve()` actually installs — captured via a fake ClientSession
factory in the first test, called directly in the rest) rather than driving
a full stdio/streamable-http round trip, since the callback's contract is
exactly what matters here: register a pending elicitation, block on a
Future, and resolve it from `resolve_elicitation` — the same hub POST
/api/approval/execute calls through the `mcp_elicit_respond` ApprovalSpec.
"""

import asyncio

import mcp
import mcp.client.stdio
import pytest

from server.approval import registry as approval_registry
from server.approval.bootstrap import register_defaults
from server.approval.spec import ApprovalOutcome
from server.mcp import MCPManager

# Populate the approval registry once for the whole module (mirrors the
# pattern used by tests/test_permission_modes.py and
# tests/test_core_low_fixes.py).
register_defaults()


class _FakeElicitParams:
    def __init__(self, message="Need your API key", mode="form", requestedSchema=None, url=None):
        self.message = message
        self.mode = mode
        self.requestedSchema = requestedSchema or {
            "type": "object",
            "properties": {"key": {"type": "string"}},
        }
        self.url = url


# --- Wiring: _serve() really does install a working callback --------------


class _FakeTool:
    def __init__(self, name):
        self.name = name
        self.description = "desc"
        self.inputSchema = {"type": "object"}


class _FakeToolsResult:
    def __init__(self):
        self.tools = [_FakeTool("ping")]


class _FakeSession:
    def __init__(self, record, elicitation_callback):
        record["elicitation_callback"] = elicitation_callback

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        pass

    async def list_tools(self):
        return _FakeToolsResult()

    async def list_resources(self):
        raise RuntimeError("no resources")


class _FakeStdioCtx:
    async def __aenter__(self):
        return ("read", "write")

    async def __aexit__(self, *exc):
        return False


def test_serve_wires_a_working_elicitation_callback_into_client_session(monkeypatch):
    record: dict = {}
    monkeypatch.setattr(
        mcp,
        "ClientSession",
        lambda r, w, elicitation_callback=None: _FakeSession(record, elicitation_callback),
        raising=False,
    )
    monkeypatch.setattr(
        mcp.client.stdio, "stdio_client", lambda params: _FakeStdioCtx(), raising=False
    )
    monkeypatch.setattr(
        MCPManager, "_resolve_command", staticmethod(lambda command, path=None: command)
    )
    mgr = MCPManager()

    async def scenario():
        await mgr.start_server("srv", {"command": "fake"})
        callback = record["elicitation_callback"]
        assert callback is not None

        task = asyncio.create_task(callback(None, _FakeElicitParams()))
        await asyncio.sleep(0)
        pending = mgr.get_pending_elicitations()
        assert len(pending) == 1
        eid = pending[0]["elicitation_id"]
        assert mgr.resolve_elicitation(eid, "accept", {"key": "abc"}) is True
        result = await task
        assert result.action == "accept"
        assert result.content == {"key": "abc"}

    asyncio.run(scenario())


# --- Human answer flows back correctly ------------------------------------


def test_human_answer_flows_back_through_pending_registry():
    mgr = MCPManager()
    callback = mgr._build_elicitation_callback("srv")

    async def scenario():
        task = asyncio.create_task(callback(None, _FakeElicitParams(message="Enter your API key")))
        await asyncio.sleep(0)  # let the callback register itself

        pending = mgr.get_pending_elicitations()
        assert len(pending) == 1
        assert pending[0]["server"] == "srv"
        assert pending[0]["message"] == "Enter your API key"
        assert pending[0]["mode"] == "form"
        eid = pending[0]["elicitation_id"]

        ok = mgr.resolve_elicitation(eid, "accept", {"key": "abc123"})
        assert ok is True

        result = await task
        assert result.action == "accept"
        assert result.content == {"key": "abc123"}
        # Cleaned up once resolved — nothing left dangling.
        assert mgr.get_pending_elicitations() == []

    asyncio.run(scenario())


def test_decline_answer_carries_no_content():
    mgr = MCPManager()
    callback = mgr._build_elicitation_callback("srv")

    async def scenario():
        task = asyncio.create_task(callback(None, _FakeElicitParams()))
        await asyncio.sleep(0)
        eid = mgr.get_pending_elicitations()[0]["elicitation_id"]
        mgr.resolve_elicitation(eid, "decline")
        result = await task
        assert result.action == "decline"
        assert result.content is None

    asyncio.run(scenario())


def test_resolve_elicitation_unknown_id_returns_false():
    mgr = MCPManager()
    assert mgr.resolve_elicitation("does-not-exist", "accept") is False


def test_resolve_elicitation_is_idempotent_second_call_is_a_noop():
    mgr = MCPManager()
    callback = mgr._build_elicitation_callback("srv")

    async def scenario():
        task = asyncio.create_task(callback(None, _FakeElicitParams()))
        await asyncio.sleep(0)
        eid = mgr.get_pending_elicitations()[0]["elicitation_id"]
        assert mgr.resolve_elicitation(eid, "accept", {"key": "first"}) is True
        result = await task
        assert result.content == {"key": "first"}
        # The id is gone once resolved; a second attempt reports failure
        # rather than silently overwriting an already-delivered answer.
        assert mgr.resolve_elicitation(eid, "accept", {"key": "second"}) is False

    asyncio.run(scenario())


# --- Timeout ----------------------------------------------------------------


def test_timeout_returns_decline_and_cleans_up(monkeypatch):
    monkeypatch.setattr("server.mcp.ELICITATION_TIMEOUT_S", 0.05)
    mgr = MCPManager()
    callback = mgr._build_elicitation_callback("srv")

    async def scenario():
        result = await callback(None, _FakeElicitParams())
        assert result.action == "decline"
        assert mgr.get_pending_elicitations() == []

    asyncio.run(scenario())


# --- Unattended-agent guard (refuse_if_agent-style) -------------------------


def test_agent_context_auto_declines_with_no_pending_record():
    """An in-flight call attributed to an unattended subagent (no human
    present) must not get a chance to answer its own elicitation — it is
    auto-declined immediately, mirroring refuse_if_agent's behavior for
    other human-gated approvals."""
    mgr = MCPManager()
    mgr._active_calls["srv"] = [{"session_id": "agent-run-1", "agent": True}]
    callback = mgr._build_elicitation_callback("srv")

    async def scenario():
        result = await callback(None, _FakeElicitParams())
        assert result.action == "decline"
        # Nothing was ever registered for a human to see or answer.
        assert mgr.get_pending_elicitations() == []

    asyncio.run(scenario())


def test_attended_chat_context_is_not_auto_declined():
    """The counterpart: a call attributed to an ordinary (non-agent) context
    DOES get routed to the pending registry for a human to answer."""
    mgr = MCPManager()
    mgr._active_calls["srv"] = [{"session_id": "chat-session-1", "agent": False}]
    callback = mgr._build_elicitation_callback("srv")

    async def scenario():
        task = asyncio.create_task(callback(None, _FakeElicitParams()))
        await asyncio.sleep(0)
        pending = mgr.get_pending_elicitations()
        assert len(pending) == 1
        assert pending[0]["session_id"] == "chat-session-1"
        mgr.resolve_elicitation(pending[0]["elicitation_id"], "cancel")
        result = await task
        assert result.action == "cancel"

    asyncio.run(scenario())


# --- Approval-pipeline plumbing: mcp_elicit_respond ApprovalSpec -----------


def test_mcp_elicit_respond_executor_resolves_the_pending_elicitation(monkeypatch):
    """POST /api/approval/execute -> registry.get('mcp_elicit_respond') ->
    the registered executor is the single hub that answers a pending
    elicitation, exactly like every other approval action."""
    from server import mcp as mcp_module

    mgr = MCPManager()
    monkeypatch.setattr(mcp_module, "mcp_manager", mgr)
    callback = mgr._build_elicitation_callback("srv")

    async def scenario():
        task = asyncio.create_task(callback(None, _FakeElicitParams()))
        await asyncio.sleep(0)
        eid = mgr.get_pending_elicitations()[0]["elicitation_id"]

        spec = approval_registry.get("mcp_elicit_respond")
        assert spec is not None
        outcome: ApprovalOutcome = await spec.executor(
            {"elicitation_id": eid, "response_action": "accept", "content": {"key": "z"}}
        )
        assert outcome.ok is True

        result = await task
        assert result.action == "accept"
        assert result.content == {"key": "z"}

    asyncio.run(scenario())


def test_mcp_elicit_respond_executor_refuses_when_agent_stamped():
    """Defense in depth: even though the PRIMARY guard lives in the
    callback itself (it never creates a pending record for an agent-
    attributed call), the executor also honors refuse_if_agent — matching
    every other human-gated approval in this codebase."""
    spec = approval_registry.get("mcp_elicit_respond")
    assert spec is not None

    async def scenario():
        outcome: ApprovalOutcome = await spec.executor(
            {"elicitation_id": "whatever", "response_action": "accept", "__agent__": True}
        )
        assert outcome.ok is False
        assert "unattended subagent" in outcome.error

    asyncio.run(scenario())


def test_mcp_elicit_respond_executor_reports_unknown_id():
    spec = approval_registry.get("mcp_elicit_respond")

    async def scenario():
        outcome: ApprovalOutcome = await spec.executor(
            {"elicitation_id": "ghost", "response_action": "accept"}
        )
        assert outcome.ok is False

    asyncio.run(scenario())
