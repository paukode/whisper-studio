"""run_headless_turn: per-round tool catalog (a tool_search activation shows up
next round) and attended mode (approvals pause the turn; resume continues it).
Same fixtures as tests/test_headless_turn.py: FakeBedrockClient scripts the real
AnthropicAdapter, fake route_tool stands in for execution."""

import asyncio
import json

from server.chat.engine.pause import paused_sessions
from server.exec.headless import run_headless_turn
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block

CFG = {
    "chat_models": {"sonnet": "test-model"},
    "default_chat_model": "sonnet",
    "permission_mode": "default",
    "permission_explainer_enabled": False,
}


async def _collect(gen):
    return [event async for event in gen]


def _patch_common(monkeypatch, fake_stream, catalog):
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    monkeypatch.setattr("server.chat.tool_pool.assemble_partitioned_pool", catalog)
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    from server.infrastructure import config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: dict(CFG))


def test_tool_catalog_is_reassembled_every_round(monkeypatch):
    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "tool_search", {"query": "git"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [
                msg_start(),
                *tool_use_block("t2", "git_branch_list", {}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("main, sonic"), *msg_end(stop_reason="end_turn")],
        ]
    )
    base = [
        {"name": "tool_search", "description": "d", "input_schema": {"type": "object"}},
    ]
    extra = {"name": "git_branch_list", "description": "d", "input_schema": {"type": "object"}}
    calls = {"n": 0}

    def catalog(*a, **k):
        calls["n"] += 1
        # Call 1 is the upfront assembly, call 2 is round 1's catalog; from
        # round 2 on, pretend tool_search activated the extra tool.
        tools = base if calls["n"] <= 2 else [*base, extra]
        return tools, [], len(tools)

    _patch_common(monkeypatch, fake_stream, catalog)

    async def fake_route_tool(tool_name, tool_input, **kw):
        return f"{tool_name} ok", []

    monkeypatch.setattr("server.tool_executor.route_tool", fake_route_tool)

    events = asyncio.run(
        _collect(
            run_headless_turn(
                "list branches", model_key="sonnet", ephemeral=True, session_id="sess-cat"
            )
        )
    )
    assert events[-1]["status"] == "completed"
    # upfront + one per round (3 rounds)
    assert calls["n"] >= 4
    # Round 2's request advertised the newly activated tool; round 1's did not.
    names_by_round = [[t["name"] for t in req.get("tools", [])] for req in fake_stream.requests]
    assert "git_branch_list" not in names_by_round[0]
    assert "git_branch_list" in names_by_round[1]


def test_attended_run_pauses_on_approval_and_resumes(monkeypatch):
    from server.approval.bootstrap import register_defaults

    register_defaults()
    sid = "sess-attended"
    paused_sessions.pop(f"exec:{sid}", None)

    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "ws_run_command", {"command": "pytest -q"}),
                *msg_end(stop_reason="tool_use"),
            ]
        ]
    )
    tools = [{"name": "ws_run_command", "description": "d", "input_schema": {"type": "object"}}]
    _patch_common(monkeypatch, fake_stream, lambda *a, **k: (tools, [], 1))

    async def fake_route_tool(tool_name, tool_input, **kw):
        payload = json.dumps({"action": "command", "command": tool_input["command"], "cwd": "/tmp"})
        return f"[WS_APPROVAL]{payload}", []

    monkeypatch.setattr("server.tool_executor.route_tool", fake_route_tool)

    events = asyncio.run(
        _collect(
            run_headless_turn(
                "run the tests", model_key="sonnet", ephemeral=True, session_id=sid, attended=True
            )
        )
    )
    types = [e["type"] for e in events]
    assert "approval_request" in types
    req = next(e for e in events if e["type"] == "approval_request")
    assert req["tool_use_id"] == "t1" and req["action"] == "command" and req["category"] == "cli"
    assert "pytest -q" in req["summary"] or req["payload"].get("command") == "pytest -q"
    assert events[-1] == {"type": "done", "status": "paused", "session_id": sid}
    assert f"exec:{sid}" in paused_sessions

    # Resume with the decision: the stashed messages come back with the
    # placeholder tool_result filled in, then the model finishes.
    fake_stream2 = FakeBedrockClient(
        [[msg_start(), *text_block("Done, all green."), *msg_end(stop_reason="end_turn")]]
    )
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream2)
    events2 = asyncio.run(
        _collect(
            run_headless_turn(
                "",
                model_key="sonnet",
                ephemeral=True,
                session_id=sid,
                attended=True,
                resume={
                    "answers": [
                        {
                            "tool_use_id": "t1",
                            "content": "[User approved] command: pytest -q. The action succeeded.",
                        }
                    ]
                },
            )
        )
    )
    assert [e["type"] for e in events2][-2:] == ["usage", "done"]
    assert events2[-1]["status"] == "completed"
    assert any(e.get("text") == "Done, all green." for e in events2)
    assert f"exec:{sid}" not in paused_sessions
    sent = fake_stream2.requests[0]["messages"]
    last_user = sent[-1]
    assert last_user["role"] == "user"
    assert last_user["content"][0]["tool_use_id"] == "t1"
    assert last_user["content"][0]["content"].startswith("[User approved]")


def test_resume_without_pause_is_an_error(monkeypatch):
    _patch_common(monkeypatch, FakeBedrockClient([]), lambda *a, **k: ([], [], 0))
    events = asyncio.run(
        _collect(
            run_headless_turn(
                "",
                model_key="sonnet",
                ephemeral=True,
                session_id="nothing-paused",
                attended=True,
                resume={"answers": []},
            )
        )
    )
    assert events[0]["type"] == "error" and "Nothing to resume" in events[0]["message"]
    assert events[-1]["status"] == "failed"
