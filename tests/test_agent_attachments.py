"""Subagents must see the parent session's durable attachments.

_run_agent_loop used to hardcode attachments=None when routing every tool
call, so analyze_document (and any other attachment-aware tool) always saw
an empty store inside a spawned agent, regardless of what was actually
attached to the session — confirmed live: a spawn_agent-created pdf_extractor
got "No documents attached." even though the parent session had a PDF
attached. Fixed by loading the same session's attachments the interactive
chat path already uses (server.attachment_store.load_session_attachments),
keyed off the session_id spawn_agent already threads through for messaging.
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
    def __init__(self, responses: list[dict]):
        self._responses = list(responses)

    def invoke_model(self, **kwargs):
        return {"body": _FakeBody(self._responses.pop(0))}


def _one_round_agent_response():
    return _FakeBedrock(
        [
            {
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_doc1",
                        "name": "analyze_document",
                        "input": {"filename": "report.pdf", "question": "summarize"},
                    }
                ],
            },
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Done."}],
            },
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
    monkeypatch.setattr("server.chat._get_bedrock_client", lambda: _one_round_agent_response())
    monkeypatch.setattr("server.chat.assemble_tool_pool", lambda *a, **k: [])
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)

    route_tool = AsyncMock(return_value=("DOCUMENT [report.pdf]:\nquarterly numbers", []))
    monkeypatch.setattr("server.tool_router.route_tool", route_tool)

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
    monkeypatch.setattr("server.chat._get_bedrock_client", lambda: _one_round_agent_response())
    monkeypatch.setattr("server.chat.assemble_tool_pool", lambda *a, **k: [])
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)

    route_tool = AsyncMock(return_value=("Document 'report.pdf' not found.", []))
    monkeypatch.setattr("server.tool_router.route_tool", route_tool)

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
