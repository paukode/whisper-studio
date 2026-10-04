"""Agent turn/time budget: the Settings limits + graceful finalize.

Two behaviours guarded here:

1. Every agent type runs on the one round limit and time limit in Settings
   (server/infrastructure/run_limits.py, resolved by get_agent_config): no
   preset carries numbers of its own, the coordinator gets twice the time,
   and the internal memory agents keep their own small round caps.

2. When an agent exhausts its turn budget the loop must still yield a usable
   result: for a schema caller it distills structured output (previously the
   turn-limit path skipped distillation, so `structured_output` came back None
   and surfaced to workflow scripts as a null `agent()` result); for a plain
   caller it does one no-tools pass to extract a final answer.
"""

import asyncio
import json
from unittest.mock import AsyncMock

from server.agent_tools.schemas import SPAWN_AGENT_TOOL, TEAM_CREATE_TOOL
from server.agents.config import AGENT_TYPES, AgentConfig, get_agent_config, with_run_limits
from server.agents.runtime import run_agent
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block

# ── every type runs on the Settings limits ───────────────────────────────────


def _settings(monkeypatch, **values) -> None:
    monkeypatch.setattr("server.infrastructure.config.load_config", lambda: dict(values))


def _internal_types() -> list[str]:
    return sorted(name for name, cfg in AGENT_TYPES.items() if cfg.internal)


def test_no_preset_carries_a_limit_of_its_own():
    """One control: a preset that hard-coded rounds or time would quietly
    override Settings, the way a per-model region pin did. Only an internal
    preset sets a round cap."""
    for name, preset in AGENT_TYPES.items():
        assert preset.deadline_seconds is None, name
        assert (preset.max_turns is not None) == preset.internal, name


def test_every_spawnable_type_takes_the_settings_limits(monkeypatch):
    _settings(monkeypatch, round_limit=33, time_limit_minutes=4)
    for name, preset in AGENT_TYPES.items():
        if preset.internal:
            continue
        cfg = get_agent_config(name)
        assert cfg.max_turns == 33, name
        assert cfg.deadline_seconds == 4 * 60 * preset.time_limit_factor, name


def test_the_coordinator_gets_twice_the_time_limit(monkeypatch):
    _settings(monkeypatch, round_limit=33, time_limit_minutes=4)
    coordinator = get_agent_config("coordinator")
    general = get_agent_config("general")
    assert coordinator.deadline_seconds == 2 * general.deadline_seconds
    assert coordinator.max_turns == general.max_turns


def test_internal_presets_keep_their_rounds_and_take_the_settings_time(monkeypatch):
    for limit in (1, 500):
        _settings(monkeypatch, round_limit=limit, time_limit_minutes=4)
        for name in _internal_types():
            cfg = get_agent_config(name)
            assert cfg.max_turns == AGENT_TYPES[name].max_turns, name
            assert cfg.deadline_seconds == 4 * 60, name


def test_invalid_settings_read_as_the_shipped_defaults(monkeypatch):
    from server.infrastructure.config import DEFAULTS

    for bad in (0, -5, "abc", None, True):
        _settings(monkeypatch, round_limit=bad, time_limit_minutes=bad)
        cfg = get_agent_config("general")
        assert cfg.max_turns == DEFAULTS["round_limit"], bad
        assert cfg.deadline_seconds == 60 * DEFAULTS["time_limit_minutes"], bad


def test_the_retired_agent_limits_block_has_no_effect(monkeypatch):
    _settings(
        monkeypatch,
        round_limit=33,
        time_limit_minutes=4,
        agent_limits={"default": {"max_turns": 7}, "explore": {"deadline_seconds": None}},
    )
    explore = get_agent_config("explore")
    assert (explore.max_turns, explore.deadline_seconds) == (33, 4 * 60)


def test_a_limit_set_on_a_config_built_in_code_is_kept(monkeypatch):
    _settings(monkeypatch, round_limit=33, time_limit_minutes=4)
    cfg = with_run_limits(AgentConfig(agent_type="general", max_turns=2, deadline_seconds=5))
    assert (cfg.max_turns, cfg.deadline_seconds) == (2, 5)


def test_memory_presets_are_internal_and_never_offered_to_the_model():
    internal = set(_internal_types())
    assert {"memory_extractor", "memory_consolidator", "session_summarizer"} <= internal
    offered = set(SPAWN_AGENT_TOOL["input_schema"]["properties"]["agent_type"]["enum"])
    member = TEAM_CREATE_TOOL["input_schema"]["properties"]["agents"]["items"]
    offered |= set(member["properties"]["agent_type"]["enum"])
    assert offered and offered.isdisjoint(internal)


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
            [
                msg_start(),
                *tool_use_block("t1", "ws_grep", {"pattern": "x"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("partial progress"), *msg_end(stop_reason="end_turn")],
        ]
    )
    _patch_common(monkeypatch, fake_stream)

    # The distillation pass (OLD adapter system) offers emit_result, never forced.
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
    # The distillation call left the choice to the model (tool_choice auto),
    # no retry needed since it validated on the first attempt.
    assert fake_old.requests[-1]["tool_choice"] == {"type": "auto"}
    assert len(fake_old.requests) == 1


def test_turn_limit_finalizes_text_when_no_schema(monkeypatch):
    # Round 0: a tool call. Round 1 is the forced-no-tools last round — the
    # model has nothing left to call, so its own answer IS the final text
    # (the engine's own last-round handling replaces the pre-migration
    # loop's separate manual "out of budget, give your best answer" pass).
    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "ws_grep", {"pattern": "x"}),
                *msg_end(stop_reason="tool_use"),
            ],
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


# ── an internal run ends at its own cap ───────────────────────────────────────


def test_consolidator_run_ends_at_its_own_cap_under_a_larger_round_limit(monkeypatch, tmp_path):
    """The field case end to end: a consolidator whose model never stops
    calling tools ends at its own cap, not at the round limit in Settings
    that every spawned agent gets."""
    cap = AGENT_TYPES["memory_consolidator"].max_turns
    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block(f"t{i}", "memory_list", {"page": i}),
                *msg_end(stop_reason="tool_use"),
            ]
            for i in range(cap + 5)
        ]
    )
    _patch_common(monkeypatch, fake_stream)
    monkeypatch.setattr("server.infrastructure.run_limits.round_limit", lambda: cap + 100)
    monkeypatch.setattr("server.agents.journal.storage_root", lambda: str(tmp_path / "storage"))
    monkeypatch.setattr("server.costs.budget.check_budget", lambda session_id: None)

    result = asyncio.run(
        run_agent(
            "consolidate the global tier",
            agent_type="memory_consolidator",
            session_id="dream",
            depth=1,
            model_id_override="test-model",
            cost_source="memory",
        )
    )
    assert result.turns_used == cap
    assert len(fake_stream.requests) == cap
    assert result.stop_reason == "turn_limit"
