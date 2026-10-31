"""Unattended consumers cannot turn compaction errors into successful runs."""

import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from server.chat import compaction
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block


@pytest.mark.parametrize("partial", [False, True])
def test_headless_compaction_failure_keeps_prompt_and_skips_distillation(monkeypatch, partial):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.exec.headless import run_headless_turn
    from server.infrastructure.sessions import router
    from tests.test_engine_salvage import RejectingThenServingClient
    from tests.test_headless_turn import _collect, _patch_common

    client = (
        FakeBedrockClient([[msg_start(), *text_block("Partial answer"), *msg_end("max_tokens")]])
        if partial
        else RejectingThenServingClient([], rejections=99)
    )
    _patch_common(monkeypatch, client)
    distill = AsyncMock(side_effect=AssertionError("no new model request after failure"))
    monkeypatch.setattr("server.agents.runtime_support.distill_run_output", distill)

    async def fail(*args, **kwargs):
        raise compaction.CompactionError()

    monkeypatch.setattr(compaction, "compact_messages_with_claude", fail)
    monkeypatch.setattr(compaction, "thresholds_for", lambda *_: (1, 2))
    sid = "headless-compaction-failure"
    events = asyncio.run(
        _collect(
            run_headless_turn(
                "Keep my prompt",
                model_key="sonnet",
                session_id=sid,
                output_schema={"type": "object"},
            )
        )
    )
    assert events[-1] == {"type": "done", "status": "failed", "session_id": sid}
    assert sum(event["type"] == "done" for event in events) == 1
    assert any("Context compaction failed" in event.get("message", "") for event in events)
    assert not any(event["type"] == "structured" for event in events)
    if partial:
        assert any(event.get("text") == "Partial answer" for event in events)
        assert len(client.requests) == 1
    else:
        assert client.invokes == 1
    distill.assert_not_called()
    app = FastAPI()
    app.include_router(router)
    saved = TestClient(app).get(f"/api/sessions/{sid}").json()["chatHistory"]
    assert len(saved) == 1
    assert saved[0]["role"] == "user" and saved[0]["content"] == "Keep my prompt"


def test_agent_compaction_failure_keeps_partial_report_and_journal(monkeypatch):
    from server.agents.config import AgentConfig
    from server.agents.journal import AgentJournal, load
    from server.agents.runtime import _run_agent_loop
    from tests.test_compaction_failure_paths import _history

    client = FakeBedrockClient(
        [[msg_start(), *text_block("Partial report"), *msg_end("max_tokens")]]
    )
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: client)
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    distill = AsyncMock(side_effect=AssertionError("no distillation after context failure"))
    monkeypatch.setattr("server.agents.runtime.distill_run_output", distill)
    emitted = []
    monkeypatch.setattr(
        "server.agents.runtime.event_bus.publish", lambda channel, event: emitted.append(event)
    )
    history = _history()
    original = copy.deepcopy(history)
    seen = []

    async def fail(messages, *args, **kwargs):
        seen.append(copy.deepcopy(messages))
        raise compaction.CompactionError()

    monkeypatch.setattr(compaction, "compact_messages_with_claude", fail)
    monkeypatch.setattr(compaction, "thresholds_for", lambda *_: (1, 2))
    sid, aid = "agent-compaction-failure", "compaction-agent"
    journal = AgentJournal.open(
        aid,
        session_id=sid,
        task="continue",
        agent_type="general",
        model="test-model",
        max_turns=20,
        deadline_seconds=None,
    )
    result = asyncio.run(
        _run_agent_loop(
            agent_id=aid,
            task="continue",
            context="",
            config=AgentConfig(agent_type="general", max_turns=20),
            model_id="test-model",
            session_id=sid,
            parent_agent_id=None,
            depth=0,
            resume_messages=history,
            journal=journal,
            structured_schema={"type": "object"},
        )
    )
    assert result.status == "failed" and result.stop_reason == "error"
    assert "Partial report" in result.output and "Context compaction failed" in result.output
    assert len(client.requests) == 1
    assert history == original and seen[0][: len(original)] == original
    distill.assert_not_called()
    assert not any(event.get("phase") == "completed" for event in emitted)
    assert any(event.get("phase") == "failed" for event in emitted)
    record = load(aid, sid)
    assert record["messages"][: len(original)] == original
    assert "Partial report" in json.dumps(record)
    assert record["meta"]["status"] == "failed"
    assert record["meta"]["stop_reason"] == "error"
