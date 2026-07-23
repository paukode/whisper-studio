"""Security prerequisites for broad GitHub capability (spec §1.1, §1.2).

1. The subagent auto-approve path (server/agents/runtime.py) executes
   `[WS_APPROVAL]` actions with no human and no category check. These tests
   pin the `__agent__` stamp mechanism + `refuse_if_agent` helper that lets a
   high-blast-radius executor refuse unattended mutation.
2. The sandbox must never inherit a GitHub token into a network-open child.
"""

import asyncio

import server.sandbox as sandbox
from server.approval import registry
from server.approval.spec import ApprovalOutcome, ApprovalSpec, refuse_if_agent
from server.tool_executor import _execute_ws_approval_inline


def test_refuse_if_agent():
    assert refuse_if_agent({}) is None
    assert refuse_if_agent({"foo": 1}) is None
    out = refuse_if_agent({"__agent__": True}, what="Closing PR #2")
    assert out is not None and out.ok is False
    assert "unattended subagent" in out.error
    assert "Closing PR #2" in out.error


def test_merged_env_strips_github_tokens(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    monkeypatch.setenv("GITHUB_TOKEN", "gho_x")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "ghe_x")
    monkeypatch.setenv("WS_KEEP_ME", "yes")

    env = sandbox._merged_env(None)
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GH_ENTERPRISE_TOKEN" not in env
    assert env["WS_KEEP_ME"] == "yes"  # non-credential vars survive

    env2 = sandbox._merged_env({"EXTRA": "1"})
    assert env2["EXTRA"] == "1"
    assert "GH_TOKEN" not in env2  # denylist still applies with extras


def test_agent_flag_stamps_payload_only_on_agent_path():
    captured: list[dict] = []

    def fake_exec(payload):
        captured.append(dict(payload))
        return ApprovalOutcome(ok=True, output="ok")

    registry.register(
        "test_agent_stamp",
        ApprovalSpec(category="test", preview="text", summary="t", executor=fake_exec),
    )

    # Subagent path stamps __agent__ so an executor can refuse.
    asyncio.run(_execute_ws_approval_inline({"action": "test_agent_stamp", "x": 1}, agent=True))
    assert captured[-1].get("__agent__") is True

    # Chat / auto-mode path (default) does NOT stamp — a real user or the
    # auto-mode classifier authorised it.
    asyncio.run(_execute_ws_approval_inline({"action": "test_agent_stamp", "x": 1}))
    assert "__agent__" not in captured[-1]


def test_agent_stamp_survives_payload_field_whitelist():
    """A spec with payload_fields whitelists inputs; the stamp is applied after
    build_payload, so it must still reach the executor."""
    captured: list[dict] = []

    def fake_exec(payload):
        captured.append(dict(payload))
        return ApprovalOutcome(ok=True, output="ok")

    registry.register(
        "test_agent_whitelist",
        ApprovalSpec(
            category="test",
            preview="text",
            summary="t",
            executor=fake_exec,
            payload_fields=["x", "session_id"],  # note: __agent__ not listed
        ),
    )
    asyncio.run(_execute_ws_approval_inline({"action": "test_agent_whitelist", "x": 1}, agent=True))
    assert captured[-1].get("__agent__") is True
    assert captured[-1].get("x") == 1
