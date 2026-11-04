"""run_headless_turn: agent progress relay (team_progress events reach the
caller, in order, between the spawning tool_call and its tool_result) and the
scope_id keyword (turn state per run while session_id keeps driving the
TurnContext). Same fixtures as tests/test_headless_turn.py: FakeBedrockClient
scripts the real AnthropicAdapter, a fake route_tool stands in for execution."""

import asyncio
import json

from server.approval.markers import approval_request
from server.chat.engine.pause import paused_sessions
from server.exec.headless import run_headless_turn
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block

CFG = {
    "chat_models": {"sonnet": "test-model"},
    "default_chat_model": "sonnet",
    "permission_mode": "default",
    "permission_explainer_enabled": False,
}


async def _collect(gen):
    return [event async for event in gen]


def _patch_common(monkeypatch, fake_stream, tools):
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool",
        lambda *a, **k: (tools, [], len(tools)),
    )
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    from server.infrastructure import config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: dict(CFG))


# ── team_progress relay ───────────────────────────────────────────────────────

# What spawn_agent (team_started / team_completed) and the agent runtime's
# _emit (everything else) publish on server.agents.event_bus for the session
# the tool batch runs under. Untyped dicts: the engine wraps each one verbatim
# as {"team_progress": ev}.
AGENT_EVENTS = [
    {
        "phase": "team_started",
        "team_id": "team1",
        "team_name": "audit",
        "description": "",
        "agents": [{"name": "audit", "task": "audit the repo", "agent_type": "general"}],
    },
    {
        "agent_id": "a1",
        "agent_name": "audit",
        "agent_type": "general",
        "team_id": "team1",
        "parent_agent_id": None,
        "phase": "started",
        "task": "audit the repo",
        "model": "test-model",
        "max_turns": 10,
    },
    {
        "agent_id": "a1",
        "agent_name": "audit",
        "agent_type": "general",
        "team_id": "team1",
        "parent_agent_id": None,
        "phase": "tool_call",
        "tool_name": "ws_grep",
        "tool_input_preview": '{"pattern": "TODO"}',
    },
    {
        "agent_id": "a1",
        "agent_name": "audit",
        "agent_type": "general",
        "team_id": "team1",
        "parent_agent_id": None,
        "phase": "completed",
        "output_preview": "3 TODOs found",
    },
    {"phase": "team_completed", "team_id": "team1", "team_name": "audit", "agents_completed": 1},
]


def test_team_progress_events_relayed_between_tool_call_and_tool_result(monkeypatch):
    from server.agents.event_bus import event_bus

    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "spawn_agent", {"task": "audit the repo"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Audit done."), *msg_end(stop_reason="end_turn")],
        ]
    )
    tools = [{"name": "spawn_agent", "description": "d", "input_schema": {"type": "object"}}]
    _patch_common(monkeypatch, fake_stream, tools)

    sid = "sess-headless-agent-events"
    dispatched_session_ids: list[str] = []

    async def fake_route_tool(tool_name, tool_input, **kw):
        # Stand-in for spawn_agent: publish on the SAME channel the real tool
        # uses (the session the batch dispatches under) while the batch runs.
        dispatched_session_ids.append(kw["session_id"])
        for ev in AGENT_EVENTS:
            event_bus.publish(kw["session_id"], dict(ev))
            await asyncio.sleep(0)
        # A typed event the long-lived session stream already delivers must
        # NOT be relabelled as agent progress (runner._route_event skips it).
        event_bus.publish(kw["session_id"], {"type": "cron_event", "phase": "done"})
        return "agent finished", []

    monkeypatch.setattr("server.tool_executor.route_tool", fake_route_tool)

    events = asyncio.run(
        _collect(
            run_headless_turn(
                "audit the repo with an agent",
                model_key="sonnet",
                ephemeral=True,
                session_id=sid,
            )
        )
    )

    types = [e["type"] for e in events]
    # The established vocabulary/order from tests/test_headless_turn.py holds,
    # with the relayed progress sitting between tool_call (+ its round's usage,
    # which the engine emits before the batch runs) and tool_result.
    assert types == [
        "tool_call",
        "usage",
        *(["team_progress"] * len(AGENT_EVENTS)),
        "tool_result",
        "text",
        "usage",
        "done",
    ]
    assert dispatched_session_ids == [sid]

    progress = [e for e in events if e["type"] == "team_progress"]
    assert [e["event"] for e in progress] == AGENT_EVENTS, (
        "each team_progress event must carry the untouched TeamProgressEvent dict, in order"
    )
    assert set(progress[0]) == {"type", "event"}
    assert [e["event"]["phase"] for e in progress] == [
        "team_started",
        "started",
        "tool_call",
        "completed",
        "team_completed",
    ]
    assert not any(e["event"].get("type") == "cron_event" for e in progress)

    assert events[0] == {
        "type": "tool_call",
        "name": "spawn_agent",
        "input": {"task": "audit the repo"},
    }
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result == {
        "type": "tool_result",
        "name": "spawn_agent",
        "output": "agent finished",
        "status": "ok",
    }
    assert events[-1] == {"type": "done", "status": "completed", "session_id": sid}


def test_no_agent_activity_yields_no_team_progress(monkeypatch):
    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "ws_grep", {"pattern": "x"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Found it."), *msg_end(stop_reason="end_turn")],
        ]
    )
    tools = [{"name": "ws_grep", "description": "d", "input_schema": {"type": "object"}}]
    _patch_common(monkeypatch, fake_stream, tools)

    async def fake_route_tool(tool_name, tool_input, **kw):
        return "matches found", []

    monkeypatch.setattr("server.tool_executor.route_tool", fake_route_tool)

    events = asyncio.run(
        _collect(
            run_headless_turn(
                "search", model_key="sonnet", ephemeral=True, session_id="sess-no-agents"
            )
        )
    )
    assert [e["type"] for e in events] == [
        "tool_call",
        "usage",
        "tool_result",
        "text",
        "usage",
        "done",
    ]


# ── scope_id: turn state per run, session_id everywhere else ──────────────────


def test_scope_id_keys_the_turn_state_while_session_id_drives_the_context(monkeypatch):
    import server.chat.engine.runner as runner_mod

    sid = "sess-shared"
    scope = "run-one"
    fake_stream = FakeBedrockClient(
        [[msg_start(), *text_block("Done."), *msg_end(stop_reason="end_turn")]]
    )
    _patch_common(monkeypatch, fake_stream, [])

    real_run_turn = runner_mod.run_turn
    captured: list[dict] = []

    async def _spy_run_turn(ctx):
        captured.append({"session_id": ctx.session_id, "turn_scope_id": ctx.turn_scope_id})
        async for chunk in real_run_turn(ctx):
            yield chunk

    monkeypatch.setattr(runner_mod, "run_turn", _spy_run_turn)

    events = asyncio.run(
        _collect(
            run_headless_turn(
                "hi", model_key="sonnet", ephemeral=True, session_id=sid, scope_id=scope
            )
        )
    )
    assert events[-1] == {"type": "done", "status": "completed", "session_id": sid}
    assert captured == [{"session_id": sid, "turn_scope_id": f"exec:{scope}"}]


def test_a_headless_run_caches_its_prompt_prefix(monkeypatch):
    """Round 1 checkpoints the tools, the system prompt and the last message;
    round 2 sends the same tools and system blocks, so it reads them from the
    cache."""
    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "ws_grep", {"pattern": "x"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Found it."), *msg_end(stop_reason="end_turn")],
        ]
    )
    tools = [{"name": "ws_grep", "description": "d", "input_schema": {"type": "object"}}]
    _patch_common(monkeypatch, fake_stream, tools)

    async def fake_route_tool(tool_name, tool_input, **kw):
        return "matches found", []

    monkeypatch.setattr("server.tool_executor.route_tool", fake_route_tool)
    asyncio.run(
        _collect(
            run_headless_turn("search", model_key="sonnet", ephemeral=True, session_id="sess-c")
        )
    )
    first, second = fake_stream.requests
    assert "cache_control" in first["tools"][-1]
    assert "cache_control" in first["system"][0]
    assert "cache_control" in str(first["messages"][-1])
    assert second["tools"] == first["tools"] and second["system"] == first["system"]


def test_session_rows_are_not_relabelled_as_agent_progress():
    """A typed row the session stream delivers (an agent report, a cron
    event) never becomes a team_progress frame; agent runtime events do."""
    from server.chat.engine.tool_rounds import _route_event

    assert _route_event({"type": "agent_report", "agentReport": {}}) is None
    assert _route_event({"type": "cron_event", "phase": "done"}) is None
    progress = {"phase": "tool_call", "agent_id": "a1"}
    assert _route_event(progress) == {"team_progress": progress}
