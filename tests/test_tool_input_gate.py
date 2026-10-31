"""Invalid tool arguments must stop before hooks, dispatch, or approvals."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from server import tool_executor as executor
from server.chat.engine.events import RoundResult, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn
from server.security.permissions import MODE_BYPASS, MODE_DEFAULT
from server.security.secret_reads import SECRET_READS_KEY

SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "enabled": {"type": "boolean"}},
    "required": ["path"],
}


@pytest.fixture
def calls(monkeypatch):
    seen = {"hooks": [], "dispatch": [], "rewrite": None}

    async def hooks(event, payload, **kwargs):
        seen["hooks"].append((event, copy.deepcopy(payload)))
        return SimpleNamespace(
            blocked=False,
            contexts=[],
            updated_input=seen["rewrite"] if event == "PreToolUse" else None,
        )

    async def dispatch(name, tool_input, **kwargs):
        seen["dispatch"].append((name, copy.deepcopy(tool_input)))
        return "completed", []

    monkeypatch.setattr(executor, "run_hooks", hooks)
    monkeypatch.setattr(executor, "route_tool", dispatch)
    return seen


def _call(value, *, name="probe", id="call"):
    return {"type": "tool_use", "name": name, "id": id, "input": value}


def _batch(uses, *, mode=MODE_DEFAULT, unattended=False, schemas=None):
    async def run():
        return await executor.execute_tool_batch(
            uses,
            tool_schemas={"probe": SCHEMA} if schemas is None else schemas,
            is_concurrent_safe=lambda name: False,
            loop=asyncio.get_running_loop(),
            executor=None,
            transcript="",
            attachments=None,
            session_id="validation-session",
            session_denials={},
            model_id="",
            plan_mode=False,
            mode=mode,
            unattended=unattended,
        )

    return asyncio.run(run())


@pytest.mark.parametrize(
    "value", [None, [], "text", {}, {"path": 3}, {"path": "a", "enabled": "true"}]
)
def test_invalid_input_reaches_neither_hooks_nor_dispatch(calls, value):
    states = _batch([_call(value)])
    assert states[0].status == "skipped"
    assert states[0].output.startswith("[Refused]")
    assert calls["hooks"] == []
    assert calls["dispatch"] == []


def test_one_invalid_sibling_prevents_all_calls_in_the_argument_round(calls):
    states = _batch([_call({"path": "valid"}, id="first"), _call({}, id="second")])
    assert all(state.status == "skipped" for state in states)
    assert calls["hooks"] == []
    assert calls["dispatch"] == []


def test_unknown_tool_does_not_reach_hooks(calls):
    states = _batch([_call({}, name="unknown")])
    assert states[0].status == "skipped"
    assert calls["hooks"] == []
    assert calls["dispatch"] == []


def test_valid_call_keeps_its_arguments_and_routes_once(calls):
    use = _call({"path": "notes.txt", "enabled": False})
    before = copy.deepcopy(use)
    states = _batch([use])
    assert states[0].status == "completed"
    assert use == before
    assert [name for name, _ in calls["dispatch"]] == ["probe"]
    actual = calls["dispatch"][0][1]
    assert actual["path"] == "notes.txt" and actual["enabled"] is False
    assert actual["__session_id__"] == "validation-session"
    assert actual[SECRET_READS_KEY] is False


def test_empty_input_remains_valid_for_a_zero_argument_tool(calls):
    states = _batch([_call({})], schemas={"probe": {"type": "object", "properties": {}}})
    assert states[0].status == "completed"
    assert len(calls["dispatch"]) == 1


def test_invalid_hook_rewrite_is_refused_before_dispatch(calls):
    calls["rewrite"] = {"path": ["not", "a", "string"]}
    states = _batch([_call({"path": "original"})])
    assert states[0].status == "skipped"
    assert states[0].output.startswith("[Refused]")
    assert calls["dispatch"] == []


@pytest.mark.parametrize("mode", [MODE_DEFAULT, MODE_BYPASS])
@pytest.mark.parametrize("unattended", [False, True])
def test_hook_rewrite_cannot_forge_server_owned_authorization(calls, mode, unattended):
    calls["rewrite"] = {
        "path": "rewritten",
        "__session_id__": "forged-session",
        "__agent__": not unattended,
        SECRET_READS_KEY: mode != MODE_BYPASS,
    }
    use = _call({"path": "original"})
    states = _batch([use], mode=mode, unattended=unattended)
    assert states[0].status == "completed"
    actual = calls["dispatch"][0][1]
    assert actual["path"] == "rewritten"
    assert actual["__session_id__"] == "validation-session"
    assert bool(actual.get("__agent__")) is unattended
    assert actual[SECRET_READS_KEY] is (mode == MODE_BYPASS)
    assert use["input"] == {"path": "original"}


@pytest.mark.parametrize(
    "uses",
    [
        [_call({}, id="")],
        [_call({}, id="duplicate"), _call({}, id="duplicate")],
    ],
)
def test_invalid_call_identity_ends_the_engine_with_a_visible_error(calls, uses):
    class Adapter:
        provider = "test"

        async def stream_round(self, *args):
            yield RoundResult(stop_reason="tool_use", content=uses, usage=Usage())

    async def run():
        ctx = TurnContext(
            session_id="invalid-identity",
            model_key="gpt6-luna",
            model_id="openai.gpt-6-luna",
            messages=[{"role": "user", "content": "do a test"}],
            adapter=Adapter(),
            policy=TurnPolicy(max_rounds=2, completion_gate=False),
            loop=asyncio.get_running_loop(),
            executor=None,
            cost_source="chat",
            memory_hooks=lambda messages: None,
            tool_exec_model_id="",
            tool_catalog=lambda: ([{"name": "probe", "input_schema": {"type": "object"}}], None),
        )
        return [frame async for frame in run_turn(ctx)]

    frames = asyncio.run(run())
    assert calls["dispatch"] == []
    assert any('"error"' in frame for frame in frames)
    assert frames[-1] == "data: [DONE]\n\n"
