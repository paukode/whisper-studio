"""Agent turn/time budget: configurable limits + graceful finalize.

Two behaviours guarded here:

1. `get_agent_config` overlays config.json `agent_limits` (default then
   type-specific) onto the built-in AGENT_TYPES preset, and a null
   `deadline_seconds` disables the wall-clock brake.

2. When an agent exhausts its turn budget the loop must still yield a usable
   result: for a schema caller it distills structured output (previously the
   turn-limit path skipped distillation, so `structured_output` came back None
   and surfaced to workflow scripts as a null `agent()` result); for a plain
   caller it does one no-tools pass to extract a final answer.
"""

import asyncio
import json
from unittest.mock import AsyncMock

from server.agents.config import AgentConfig, get_agent_config
from server.agents.runtime import run_agent
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block

# ── get_agent_config overrides ────────────────────────────────────────────────


def test_get_agent_config_no_overrides_keeps_preset(monkeypatch):
    monkeypatch.setattr("server.infrastructure.config.load_config", lambda: {})
    c = get_agent_config("general")
    assert c.max_turns == 120
    assert c.deadline_seconds == 900


def test_get_agent_config_default_then_type_specific(monkeypatch):
    monkeypatch.setattr(
        "server.infrastructure.config.load_config",
        lambda: {
            "agent_limits": {
                "default": {"max_turns": 200},
                "general": {"deadline_seconds": 111},
            }
        },
    )
    c = get_agent_config("general")
    assert c.max_turns == 200  # from default block
    assert c.deadline_seconds == 111  # type-specific block adds on top


def test_get_agent_config_null_disables_deadline(monkeypatch):
    monkeypatch.setattr(
        "server.infrastructure.config.load_config",
        lambda: {"agent_limits": {"explore": {"deadline_seconds": None}}},
    )
    c = get_agent_config("explore")
    assert c.deadline_seconds is None
    assert c.max_turns == 30  # preset preserved


def test_get_agent_config_ignores_invalid_values(monkeypatch):
    monkeypatch.setattr(
        "server.infrastructure.config.load_config",
        lambda: {"agent_limits": {"general": {"max_turns": 0, "deadline_seconds": -5}}},
    )
    c = get_agent_config("general")
    # Non-positive values are ignored; the preset stands.
    assert c.max_turns == 120
    assert c.deadline_seconds == 900


# ── graceful finalize at the turn limit ───────────────────────────────────────
#
# The agent loop now runs through server/chat/engine/runner.py — the same
# shared turn engine interactive chat uses — so its own model calls are
# scripted with the streaming FakeBedrockClient harness (tests/golden_
# harness.py), patched at the chat/engine adapter's own Bedrock-client
# binding. _distill_structured (unchanged, out of scope for the migration —
# chat/engine's adapters have no forced-tool-call equivalent yet) still goes
# through the OLD, non-streaming server.agents.providers adapter system, so
# it needs the OLD _FakeBedrock/_FakeBody shape patched at server.chat's own
# binding — the two are independent clients for independent call paths.


class _FakeBody:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._data


class _FakeBedrock:
    """Replays canned responses; records each request body. Backs the OLD
    non-streaming adapter system (server.agents.providers), used only by
    _distill_structured now."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.requests: list[dict] = []

    def invoke_model(self, **kwargs):
        self.requests.append(json.loads(kwargs["body"]))
        return {"body": _FakeBody(self._responses.pop(0))}


def _patch_common(monkeypatch, fake_stream):
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    monkeypatch.setattr("server.chat.assemble_tool_pool", lambda *a, **k: [])
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    monkeypatch.setattr("server.tool_executor.route_tool", AsyncMock(return_value=("ok", [])))


def test_turn_limit_distills_structured_output(monkeypatch):
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    # Round 0: a tool call. Round 1 is max_turns=2's forced-no-tools last
    # round (the engine omits tools from that request entirely — see
    # server/chat/engine/anthropic.py's is_last_round handling), so a real
    # model has nothing left to call and just answers in text.
    fake_stream = FakeBedrockClient(
        [
            [msg_start(), *tool_use_block("t1", "noop", {}), *msg_end(stop_reason="tool_use")],
            [msg_start(), *text_block("partial progress"), *msg_end(stop_reason="end_turn")],
        ]
    )
    _patch_common(monkeypatch, fake_stream)

    # The distillation pass (OLD adapter system) forces emit_result.
    structured_resp = {
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "id": "s1",
                "name": "emit_result",
                "input": {"answer": "partial but usable"},
            }
        ],
    }
    fake_old = _FakeBedrock([structured_resp])
    monkeypatch.setattr("server.chat._get_bedrock_client", lambda: fake_old)

    cfg = AgentConfig(agent_type="general", max_turns=2, deadline_seconds=None)
    result = asyncio.run(
        run_agent(
            "do it",
            config=cfg,
            session_id="",
            model_id_override="test-model",
            structured_schema=schema,
        )
    )
    assert result.structured_output == {"answer": "partial but usable"}
    assert result.status == "completed"
    assert result.stopped_early is True
    assert "turn limit" in result.output.lower()
    # The distillation call went out with the forced tool_choice, no schema
    # validation loop needed since it validated on the first attempt.
    assert fake_old.requests[-1]["tool_choice"] == {"type": "tool", "name": "emit_result"}


def test_turn_limit_finalizes_text_when_no_schema(monkeypatch):
    # Round 0: a tool call. Round 1 is the forced-no-tools last round — the
    # model has nothing left to call, so its own answer IS the final text
    # (the engine's own last-round handling replaces the pre-migration
    # loop's separate manual "out of budget, give your best answer" pass).
    fake_stream = FakeBedrockClient(
        [
            [msg_start(), *tool_use_block("t1", "noop", {}), *msg_end(stop_reason="tool_use")],
            [
                msg_start(),
                *text_block("Here is my best summary so far."),
                *msg_end(stop_reason="end_turn"),
            ],
        ]
    )
    _patch_common(monkeypatch, fake_stream)

    cfg = AgentConfig(agent_type="general", max_turns=2, deadline_seconds=None)
    result = asyncio.run(
        run_agent(
            "do it",
            config=cfg,
            session_id="",
            model_id_override="test-model",
        )
    )
    assert "best summary" in result.output
    assert result.status == "completed"
    assert result.stopped_early is True
    # The last round's request must not offer tools at all — Anthropic's
    # is_last_round handling omits the key entirely, so the model had no
    # choice but to answer in text.
    assert not fake_stream.requests[-1].get("tools")
