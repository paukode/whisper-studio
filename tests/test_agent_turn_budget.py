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


class _FakeBody:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._data


class _FakeBedrock:
    """Replays canned responses; records each request body."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.requests: list[dict] = []

    def invoke_model(self, **kwargs):
        self.requests.append(json.loads(kwargs["body"]))
        return {"body": _FakeBody(self._responses.pop(0))}


_TOOL_USE = {
    "stop_reason": "tool_use",
    "content": [{"type": "tool_use", "id": "t1", "name": "noop", "input": {}}],
}


def _patch_common(monkeypatch, fake):
    monkeypatch.setattr("server.chat._get_bedrock_client", lambda: fake)
    monkeypatch.setattr("server.chat.assemble_tool_pool", lambda *a, **k: [])
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    monkeypatch.setattr("server.tool_router.route_tool", AsyncMock(return_value=("ok", [])))


def test_turn_limit_distills_structured_output(monkeypatch):
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
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
    # Two turns of plain tool_use exhaust max_turns=2; the distill pass then
    # returns the forced emit_result. No 4th call: with structured output in
    # hand the no-tools text finalize is skipped.
    fake = _FakeBedrock([_TOOL_USE, _TOOL_USE, structured_resp])
    _patch_common(monkeypatch, fake)

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
    assert "turn limit" in result.output.lower()


def test_turn_limit_finalizes_text_when_no_schema(monkeypatch):
    final_resp = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Here is my best summary so far."}],
    }
    # Two tool-use turns (no text) exhaust max_turns=2; the no-tools finalize
    # pass then produces the final answer.
    fake = _FakeBedrock([_TOOL_USE, _TOOL_USE, final_resp])
    _patch_common(monkeypatch, fake)

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
    # The transcript contains tool_use/tool_result blocks, and Anthropic rejects
    # such requests without a tools param — the finalize call must therefore
    # carry the tool definitions (omitting them made this pass silently fail).
    assert fake.requests[-1].get("tools"), "finalize request must include tools"
