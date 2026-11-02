"""Nested calls cannot forge execution context or partially run invalid rounds."""

import asyncio

from server.chat.engine.events import RoundResult, Usage
from server.memory.review_fork import ReviewFork, run_review
from server.security.secret_reads import SECRET_READS_KEY
from server.workflows.runtime import WorkflowRun
from tests.test_engine_openai import FakeEvent, _adapter, _collect, _completed, _fast_release
from tests.test_tool_stream_rejection import _assert_rejected, _done, _start


def test_script_subcalls_replace_forged_server_owned_context(monkeypatch):
    observed = []

    def execute(name, args):
        observed.append((name, dict(args)))
        return "read result"

    monkeypatch.setattr("server.skills.execute_tool", execute)
    run = WorkflowRun("script-context-test", "", session_id="real-session")
    replies = []

    async def reply(message_id, result):
        replies.append(result)

    async def error(message_id, error_type, message):
        raise AssertionError(message)

    run._respond = reply
    run._error = error
    forged = {
        "path": "notes.txt",
        "__session_id__": "forged-session",
        "__agent__": False,
        SECRET_READS_KEY: True,
    }
    asyncio.run(run._handle_tool(1, {"name": "ws_read_file", "args": forged}))
    assert replies == [{"output": "read result"}]
    assert len(observed) == 1
    received = observed[0][1]
    assert received["path"] == "notes.txt"
    assert received["__session_id__"] == "real-session"
    assert received[SECRET_READS_KEY] is False
    assert received["__agent__"] is True
    assert forged[SECRET_READS_KEY] is True
    assert forged["__session_id__"] == "forged-session"


def test_learning_review_preflights_every_allowed_call_before_a_write(monkeypatch):
    observed = []

    async def route(name, args, **kwargs):
        observed.append((name, dict(args)))
        return "deleted", []

    monkeypatch.setattr("server.tool_router.route_tool", route)
    monkeypatch.setattr("server.costs.tracker.record_turn", lambda **kwargs: None)

    class Adapter:
        provider = "anthropic"

        async def stream_round(self, *args):
            yield RoundResult(
                stop_reason="tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": "first",
                        "name": "memory_delete",
                        "input": {"filename": "first.md"},
                    },
                    {"type": "tool_use", "id": "second", "name": "memory_delete", "input": {}},
                ],
                usage=Usage(),
            )

    schema = {
        "type": "object",
        "properties": {"filename": {"type": "string"}},
        "required": ["filename"],
    }
    fork = ReviewFork(
        adapter=Adapter(),
        tools=[{"name": "memory_delete", "input_schema": schema}],
        core_count=1,
        messages=[{"role": "user", "content": "review"}],
        model_key="probe",
        model_id="probe",
        session_id="review-context-test",
        ws_path=None,
        loop=None,
        executor=None,
    )
    asyncio.run(run_review(fork, max_rounds=1))
    assert observed == []


def test_openai_incomplete_output_item_blocks_early_release(monkeypatch):
    _fast_release(monkeypatch)
    incomplete_item = FakeEvent(
        type="response.output_item.done",
        item=FakeEvent(
            type="function_call",
            id="item",
            call_id="call",
            name="git_status",
            arguments="{}",
            status="incomplete",
        ),
    )
    adapter, _ = _adapter(
        monkeypatch,
        [
            [
                _start(name="git_status"),
                _done("{}"),
                incomplete_item,
                0.4,
                FakeEvent(
                    type="response.incomplete",
                    response=FakeEvent(incomplete_details=FakeEvent(reason="max_output_tokens")),
                ),
            ]
        ],
    )
    events = asyncio.run(_collect(adapter.stream_round([], [], None, 0, False)))
    _assert_rejected(events)


def test_final_text_cannot_early_release_a_known_unfinished_call(monkeypatch):
    _fast_release(monkeypatch)
    adapter, _ = _adapter(
        monkeypatch,
        [
            [
                _start(),
                FakeEvent(
                    type="response.output_item.added",
                    item=FakeEvent(type="message", phase="final_answer"),
                ),
                FakeEvent(type="response.output_text.delta", delta="Partial answer"),
                FakeEvent(type="response.output_text.done"),
                0.4,
                _completed(),
            ]
        ],
    )
    events = asyncio.run(_collect(adapter.stream_round([], [], None, 0, False)))
    _assert_rejected(events)
