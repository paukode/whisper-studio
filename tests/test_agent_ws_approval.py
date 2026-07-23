"""Agent runtime WS_APPROVAL auto-approve path.

_execute_ws_approval_inline is async. The agent tool loop in
server/agents/runtime.py must AWAIT it — calling it bare assigns a coroutine
object to `output`, so the gated action never runs and the model sees
"<coroutine object ...>" as the tool result (plus a never-awaited warning).

This drives the real _run_agent_loop with a fake Bedrock client: turn 1 asks
for a ws_write_file call whose routed output is a [WS_APPROVAL] sentinel,
turn 2 ends the run. The tool_result content replayed to the model on turn 2
must be the executed string, not a coroutine repr.
"""

import asyncio
import json
from unittest.mock import AsyncMock

from server.agents.runtime import run_agent


class _FakeBody:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._data


class _FakeBedrock:
    """Replays canned responses and records each request body."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.requests: list[dict] = []

    def invoke_model(self, **kwargs):
        self.requests.append(json.loads(kwargs["body"]))
        return {"body": _FakeBody(self._responses.pop(0))}


def test_ws_approval_sentinel_is_executed_not_left_as_coroutine(monkeypatch):
    ws_payload = {"action": "ws_write_file", "path": "foo.txt", "content": "hi", "original": ""}
    sentinel = "[WS_APPROVAL]" + json.dumps(ws_payload)

    fake_bedrock = _FakeBedrock(
        [
            {
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_ws1",
                        "name": "ws_write_file",
                        "input": {"path": "foo.txt", "content": "hi"},
                    }
                ],
            },
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "File written."}],
            },
        ]
    )

    monkeypatch.setattr("server.chat._get_bedrock_client", lambda: fake_bedrock)
    monkeypatch.setattr("server.chat.assemble_tool_pool", lambda *a, **k: [])
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    # The router hands back the approval sentinel (auto-mode gated tool)...
    route_tool = AsyncMock(return_value=(sentinel, []))
    monkeypatch.setattr("server.tool_router.route_tool", route_tool)
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
