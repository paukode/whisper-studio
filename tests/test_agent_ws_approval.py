"""Agent runtime WS_APPROVAL auto-approve path.

_execute_ws_approval_inline is async. The agent turn loop
(server/agents/runtime.py, now backed by server/chat/engine/runner.py) must
AWAIT it — calling it bare assigns a coroutine object to the tool output, so
the gated action never runs and the model sees "<coroutine object ...>" as
the tool result (plus a never-awaited warning).

This drives the real _run_agent_loop with a scripted Bedrock stream (the
same FakeBedrockClient/event-builder harness tests/golden_harness.py and
tests/test_engine_golden.py use for interactive chat, since the agent loop
now goes through the same chat/engine.anthropic.AnthropicAdapter): turn 1
asks for a ws_write_file call whose routed output is a [WS_APPROVAL]
sentinel, turn 2 ends the run. The tool_result content replayed to the model
on turn 2 must be the executed string, not a coroutine repr.
"""

import asyncio
import json
from unittest.mock import AsyncMock

from server.agents.runtime import run_agent
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block


def test_ws_approval_sentinel_is_executed_not_left_as_coroutine(monkeypatch):
    ws_payload = {"action": "ws_write_file", "path": "foo.txt", "content": "hi", "original": ""}
    sentinel = "[WS_APPROVAL]" + json.dumps(ws_payload)

    # process_tool_results only takes the unattended "auto-approve inline"
    # branch once an ApprovalSpec is actually registered for the action (the
    # unknown-action refusal is checked first) — register the real built-in
    # specs the way server/main.py does at startup. Idempotent.
    from server.approval.bootstrap import register_defaults

    register_defaults()

    fake_bedrock = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("tu_ws1", "ws_write_file", {"path": "foo.txt", "content": "hi"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("File written."), *msg_end(stop_reason="end_turn")],
        ]
    )

    # The agent loop's own adapter (server.chat.engine.anthropic) resolves
    # its Bedrock client independently of the chat route's — patch its OWN
    # binding, matching how tests/golden_harness.run_chat_turn does it.
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_bedrock)
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool", lambda *a, **k: ([], [], 0)
    )
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    # The router hands back the approval sentinel (auto-mode gated tool)...
    route_tool = AsyncMock(return_value=(sentinel, []))
    monkeypatch.setattr("server.tool_executor.route_tool", route_tool)
    # ...and the inline executor is the async function the loop must await.
    execute_inline = AsyncMock(return_value="[OK] wrote foo.txt")
    monkeypatch.setattr("server.tool_executor._execute_ws_approval_inline", execute_inline)

    result = asyncio.run(
        run_agent(
            "write foo.txt",
            agent_type="general",
            session_id="",
            model_id_override="test-model",
        )
    )

    # The executor ran, with the parsed sentinel payload. agent=True marks the
    # subagent origin so high-blast-radius executors can refuse unattended runs.
    execute_inline.assert_awaited_once_with(ws_payload, agent=True)

    # Turn 2's request replays the tool_result the model saw: it must be the
    # executed string, never a coroutine repr.
    assert len(fake_bedrock.requests) == 2
    tool_results = fake_bedrock.requests[1]["messages"][-1]["content"]
    assert tool_results[0]["tool_use_id"] == "tu_ws1"
    assert tool_results[0]["content"] == "[OK] wrote foo.txt"
    assert "coroutine" not in tool_results[0]["content"]

    assert result.status == "completed"
    assert result.tools_called == ["ws_write_file"]
