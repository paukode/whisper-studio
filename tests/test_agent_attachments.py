"""Subagents must see the parent session's durable attachments.

_run_agent_loop used to hardcode attachments=None when routing every tool
call, so analyze_document (and any other attachment-aware tool) always saw
an empty store inside a spawned agent, regardless of what was actually
attached to the session — confirmed live: a spawn_agent-created pdf_extractor
got "No documents attached." even though the parent session had a PDF
attached. Fixed by loading the same session's attachments the interactive
chat path already uses (server.attachment_store.load_session_attachments),
keyed off the session_id spawn_agent already threads through for messaging.

The agent loop now runs through server/chat/engine/runner.py (the same
shared turn engine interactive chat uses), so its model calls are scripted
with the same FakeBedrockClient/event-builder harness
tests/golden_harness.py and tests/test_engine_golden.py use, patched at the
chat/engine adapter's own Bedrock-client binding.
"""

import asyncio
from unittest.mock import AsyncMock

from server.agents.runtime import run_agent
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block


def _one_round_agent_response():
    return FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block(
                    "tu_doc1",
                    "analyze_document",
                    {"filename": "report.pdf", "question": "summarize"},
                ),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Done."), *msg_end(stop_reason="end_turn")],
        ]
    )


def test_spawned_agent_receives_the_session_attachments(monkeypatch):
    fixture_attachments = {
        "att_1": {"filename": "report.pdf", "kind": "document", "text": "quarterly numbers"}
    }

    monkeypatch.setattr(
        "server.attachment_store.load_session_attachments",
        lambda session_id, **k: fixture_attachments if session_id == "parent-session" else {},
    )
    monkeypatch.setattr(
        "server.chat.engine.anthropic._get_bedrock_client", lambda: _one_round_agent_response()
    )
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool", lambda *a, **k: ([], [], 0)
    )
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)

    route_tool = AsyncMock(return_value=("DOCUMENT [report.pdf]:\nquarterly numbers", []))
    monkeypatch.setattr("server.tool_executor.route_tool", route_tool)

    result = asyncio.run(
        run_agent(
            "summarize report.pdf",
            agent_type="general",
            session_id="parent-session",
            model_id_override="test-model",
        )
    )

    route_tool.assert_awaited_once()
    call_kwargs = route_tool.await_args.kwargs
    assert call_kwargs["attachments"] == fixture_attachments
    assert result.status == "completed"


def test_spawned_agent_with_no_session_id_gets_empty_attachments_not_a_crash(monkeypatch):
    # No session (e.g. an ephemeral/standalone invocation) — must degrade to
    # an empty store, not error out trying to look one up.
    monkeypatch.setattr(
        "server.attachment_store.load_session_attachments",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    monkeypatch.setattr(
        "server.chat.engine.anthropic._get_bedrock_client", lambda: _one_round_agent_response()
    )
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool", lambda *a, **k: ([], [], 0)
    )
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)

    route_tool = AsyncMock(return_value=("Document 'report.pdf' not found.", []))
    monkeypatch.setattr("server.tool_executor.route_tool", route_tool)

    result = asyncio.run(
        run_agent(
            "summarize report.pdf",
            agent_type="general",
            session_id="",
            model_id_override="test-model",
        )
    )

    call_kwargs = route_tool.await_args.kwargs
    assert call_kwargs["attachments"] == {}
    assert result.status == "completed"
