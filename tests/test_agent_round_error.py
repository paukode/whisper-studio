"""A terminal round error fails the agent run instead of completing it."""

import asyncio
from unittest.mock import AsyncMock

from tests.golden_harness import FakeBedrockClient


class _DeniedClient(FakeBedrockClient):
    def invoke_model_with_response_stream(self, **kw):
        raise Exception("AccessDeniedException: no access to this model")


def test_agent_round_error_is_a_failed_run(monkeypatch):
    from server.agents.config import AgentConfig
    from server.agents.journal import AgentJournal, load
    from server.agents.runtime import _run_agent_loop
    from server.agents.runtime_support import _agent_finished

    monkeypatch.setattr(
        "server.chat.engine.anthropic._get_bedrock_client", lambda: _DeniedClient([])
    )
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    distill = AsyncMock(side_effect=AssertionError("no distillation after a failed round"))
    monkeypatch.setattr("server.agents.runtime.distill_run_output", distill)
    emitted = []
    monkeypatch.setattr(
        "server.agents.runtime.event_bus.publish", lambda channel, event: emitted.append(event)
    )
    sid, aid = "agent-round-error", "round-error-agent"
    journal = AgentJournal.open(
        aid,
        session_id=sid,
        task="do it",
        agent_type="general",
        model="test-model",
        max_turns=20,
        deadline_seconds=None,
    )
    result = asyncio.run(
        _run_agent_loop(
            agent_id=aid,
            task="do it",
            context="",
            config=AgentConfig(agent_type="general", max_turns=20),
            model_id="test-model",
            session_id=sid,
            parent_agent_id=None,
            depth=0,
            journal=journal,
            structured_schema={"type": "object"},
        )
    )
    assert result.status == "failed" and result.stop_reason == "error"
    assert result.output.startswith("[Agent Error]")
    assert not _agent_finished(result)
    distill.assert_not_called()
    assert not any(event.get("phase") == "completed" for event in emitted)
    assert any(event.get("phase") == "failed" for event in emitted)
    meta = load(aid, sid)["meta"]
    assert meta["status"] == "failed" and meta["stop_reason"] == "error"
