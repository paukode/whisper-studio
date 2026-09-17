"""run_headless_turn: agent progress relay (team_progress events reach the
caller, in order, between the spawning tool_call and its tool_result) and the
scope_id keyword (pause isolation per run while session_id keeps driving the
TurnContext and tool dispatch). Same fixtures as tests/test_headless_turn.py
and tests/test_headless_attended.py: FakeBedrockClient scripts the real
AnthropicAdapter, a fake route_tool stands in for execution."""

import asyncio
import json

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


# ── scope_id: pause slot per run, session_id everywhere else ──────────────────


def test_scope_id_keys_the_pause_slot_while_session_id_drives_the_context(monkeypatch):
    import server.chat.engine.runner as runner_mod
    from server.approval.bootstrap import register_defaults

    register_defaults()
    sid = "sess-voice-shared"
    scope = "run-one"
    paused_sessions.pop(f"exec:{scope}", None)
    paused_sessions.pop(f"exec:{sid}", None)

    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "ws_run_command", {"command": "pytest -q"}),
                *msg_end(stop_reason="tool_use"),
            ]
        ]
    )
    tools = [{"name": "ws_run_command", "description": "d", "input_schema": {"type": "object"}}]
    _patch_common(monkeypatch, fake_stream, tools)

    dispatched_session_ids: list[str] = []

    async def fake_route_tool(tool_name, tool_input, **kw):
        dispatched_session_ids.append(kw["session_id"])
        payload = json.dumps({"action": "command", "command": tool_input["command"], "cwd": "/tmp"})
        return f"[WS_APPROVAL]{payload}", []

    monkeypatch.setattr("server.tool_executor.route_tool", fake_route_tool)

    # Spy on the real run_turn to read the built TurnContext directly.
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
                "run the tests",
                model_key="sonnet",
                ephemeral=True,
                session_id=sid,
                attended=True,
                scope_id=scope,
            )
        )
    )
    assert events[-1] == {"type": "done", "status": "paused", "session_id": sid}
    assert any(e["type"] == "approval_request" and e["tool_use_id"] == "t1" for e in events)

    # The pause slot is keyed by the scope, not the session...
    assert f"exec:{scope}" in paused_sessions
    assert f"exec:{sid}" not in paused_sessions
    # ...while the TurnContext and tool dispatch still run under session_id.
    assert captured == [{"session_id": sid, "turn_scope_id": f"exec:{scope}"}]
    assert dispatched_session_ids == [sid]

    # Resuming under the bare session (no scope_id) finds nothing: the slot
    # belongs to the scoped run and stays put.
    events_wrong = asyncio.run(
        _collect(
            run_headless_turn(
                "",
                model_key="sonnet",
                ephemeral=True,
                session_id=sid,
                attended=True,
                resume={"answers": [{"tool_use_id": "t1", "content": "[User approved]"}]},
            )
        )
    )
    assert events_wrong[0]["type"] == "error" and "Nothing to resume" in events_wrong[0]["message"]
    assert events_wrong[-1]["status"] == "failed"
    assert f"exec:{scope}" in paused_sessions

    # Resuming with the same scope_id pops the slot and finishes the turn.
    fake_stream2 = FakeBedrockClient(
        [[msg_start(), *text_block("Done, all green."), *msg_end(stop_reason="end_turn")]]
    )
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream2)
    events2 = asyncio.run(
        _collect(
            run_headless_turn(
                "",
                model_key="sonnet",
                ephemeral=True,
                session_id=sid,
                attended=True,
                scope_id=scope,
                resume={
                    "answers": [
                        {
                            "tool_use_id": "t1",
                            "content": "[User approved] command: pytest -q. The action succeeded.",
                        }
                    ]
                },
            )
        )
    )
    assert events2[-1] == {"type": "done", "status": "completed", "session_id": sid}
    assert any(e.get("text") == "Done, all green." for e in events2)
    assert f"exec:{scope}" not in paused_sessions
    assert captured[-1] == {"session_id": sid, "turn_scope_id": f"exec:{scope}"}
    sent = fake_stream2.requests[0]["messages"]
    assert sent[-1]["role"] == "user"
    assert sent[-1]["content"][0]["tool_use_id"] == "t1"
    assert sent[-1]["content"][0]["content"].startswith("[User approved]")


def test_two_runs_on_one_session_pause_independently(monkeypatch):
    from server.approval.bootstrap import register_defaults

    register_defaults()
    sid = "sess-voice-two-runs"
    for key in (f"exec:{sid}", "exec:run-a", "exec:run-b"):
        paused_sessions.pop(key, None)

    tools = [{"name": "ws_run_command", "description": "d", "input_schema": {"type": "object"}}]

    async def fake_route_tool(tool_name, tool_input, **kw):
        payload = json.dumps({"action": "command", "command": tool_input["command"], "cwd": "/tmp"})
        return f"[WS_APPROVAL]{payload}", []

    monkeypatch.setattr("server.tool_executor.route_tool", fake_route_tool)

    def _pause(scope, command):
        fake_stream = FakeBedrockClient(
            [
                [
                    msg_start(),
                    *tool_use_block(f"t-{scope}", "ws_run_command", {"command": command}),
                    *msg_end(stop_reason="tool_use"),
                ]
            ]
        )
        _patch_common(monkeypatch, fake_stream, tools)
        events = asyncio.run(
            _collect(
                run_headless_turn(
                    command,
                    model_key="sonnet",
                    ephemeral=True,
                    session_id=sid,
                    attended=True,
                    scope_id=scope,
                )
            )
        )
        assert events[-1]["status"] == "paused"

    _pause("run-a", "pytest -q")
    _pause("run-b", "ruff check .")

    # Both runs are parked under their own keys; neither clobbered the other
    # and nothing landed under the shared session key.
    assert "exec:run-a" in paused_sessions
    assert "exec:run-b" in paused_sessions
    assert f"exec:{sid}" not in paused_sessions
    assert paused_sessions["exec:run-a"]["pending_tool_results"][0]["tool_use_id"] == "t-run-a"
    assert paused_sessions["exec:run-b"]["pending_tool_results"][0]["tool_use_id"] == "t-run-b"

    paused_sessions.pop("exec:run-a", None)
    paused_sessions.pop("exec:run-b", None)


def test_event_channel_keeps_agent_progress_off_the_session_channel(monkeypatch):
    """A run with its own event channel drains agent progress from that channel
    and ignores the session channel, so a concurrent typed chat turn on the same
    session never adopts a voice run's agent cards (and vice versa)."""
    from server.agents.event_bus import event_bus

    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "spawn_agent", {"task": "audit"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Done."), *msg_end(stop_reason="end_turn")],
        ]
    )
    tools = [{"name": "spawn_agent", "description": "d", "input_schema": {"type": "object"}}]
    _patch_common(monkeypatch, fake_stream, tools)
    sid = "sess-shared"
    seen_channels: list[str | None] = []

    async def fake_route_tool(tool_name, tool_input, **kw):
        seen_channels.append(kw.get("event_channel"))
        event_bus.publish(
            kw["event_channel"], {"phase": "started", "agent_id": "a1", "team_id": "t"}
        )
        await asyncio.sleep(0)
        # Another turn's agents on the plain session channel: not ours.
        event_bus.publish(
            kw["session_id"], {"phase": "started", "agent_id": "other", "team_id": "x"}
        )
        await asyncio.sleep(0)
        return "ok", []

    monkeypatch.setattr("server.tool_executor.route_tool", fake_route_tool)

    async def collect():
        out = []
        async for ev in run_headless_turn(
            "audit", model_key="sonnet", ephemeral=True, session_id=sid, event_channel="voice:r1"
        ):
            out.append(ev)
        return out

    events = asyncio.run(collect())
    assert seen_channels == ["voice:r1"]
    progress = [e["event"] for e in events if e["type"] == "team_progress"]
    assert progress == [{"phase": "started", "agent_id": "a1", "team_id": "t"}]
