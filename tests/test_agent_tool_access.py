"""An agent runs only what it is entitled to, and is offered what it needs.

Two layers (server/agents/tool_access.py): the tools array decides what the
model is shown, and the tool executor refuses every name outside the run's
entitlement, whatever the model emits. The field case: a session summary run
offered four tools ran tool_search, memory_list and memory_write by name and
saved a long-term memory file, and the memory agents were never offered the
memory tools their whitelist names because those are deferred in chat.

These run the REAL run_agent and the real tool catalog; only the model and
the non-search tool handlers are scripted.
"""

import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

import server.tool_executor as te
from server.agents import journal as journal_mod
from server.agents.config import AGENT_TYPES, WRITE_TOOLS, AgentConfig, _is_write_tool
from server.agents.runtime import run_agent
from server.agents.tool_access import AgentToolAccess, ToolScope, activation_key
from server.agents.tools import get_agent_runtime_tools
from server.chat.tool_activation import get_ordered
from server.chat.tool_partition import core_names
from server.chat.tool_pool import assemble_full_catalog
from server.tasks import shell
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_mod, "storage_root", lambda: str(tmp_path / "storage"))
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))
    monkeypatch.setattr("server.costs.budget.check_budget", lambda session_id: None)
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)


def _patch_engine(monkeypatch, fake_stream) -> list[str]:
    """Script the model; record every call that reaches the router. The real
    router still answers tool_search, so activation is exercised for real."""
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    real_route = te.route_tool
    dispatched: list[str] = []

    async def route(name, tool_input, **kwargs):
        dispatched.append(name)
        if name == "tool_search":
            return await real_route(name, tool_input, **kwargs)
        return "ok", []

    monkeypatch.setattr(te, "route_tool", route)
    return dispatched


def _sid() -> str:
    return f"s-{uuid.uuid4().hex[:8]}"


def _names(request: dict) -> list[str]:
    return [t["name"] for t in request.get("tools") or []]


def _results(request: dict) -> list[str]:
    return [b["content"] for b in request["messages"][-1]["content"] if b["type"] == "tool_result"]


def _catalog_names() -> set[str]:
    return {t["name"] for t in assemble_full_catalog(plan_mode=False, ws_connected=False)}


def _deferred_names() -> list[str]:
    runtime = {t["name"] for t in get_agent_runtime_tools("probe", 0)}
    return sorted(_catalog_names() - core_names() - runtime)


def _done():
    return [msg_start(), *text_block("Done."), *msg_end(stop_reason="end_turn")]


# ── what an agent is offered ────────────────────────────────────────────────


WHITELISTED = sorted(name for name, cfg in AGENT_TYPES.items() if cfg.allowed_tools is not None)


@pytest.mark.parametrize("agent_type", WHITELISTED)
def test_whitelisted_agent_is_offered_its_whole_list_and_nothing_to_search(agent_type):
    access = AgentToolAccess(
        AGENT_TYPES[agent_type], agent_id="probe", depth=1, session_id=_sid(), ws_connected=False
    )
    offered = {t["name"] for t in access.offered()}
    assert offered == access.scope.permitted
    assert offered <= AGENT_TYPES[agent_type].allowed_tools
    assert access.deferred_index() == ""


def test_a_whitelisted_tool_chat_defers_is_still_offered(monkeypatch):
    """The case that broke: a whitelist that names a tool chat defers (the
    memory tools were) and leaves out tool_search had no way to reach it."""
    deferred = _deferred_names()[0]
    config = AgentConfig(agent_type="probe", allowed_tools=frozenset({deferred, "ws_read_file"}))
    fake = FakeBedrockClient([_done()])
    _patch_engine(monkeypatch, fake)
    asyncio.run(run_agent("go", config=config, session_id=_sid(), model_id_override="m"))
    offered = set(_names(fake.requests[0]))
    assert deferred in offered and "tool_search" not in offered
    assert "Additional tools (not loaded)" not in json.dumps(fake.requests[0].get("system"))


def test_spawnable_agent_keeps_its_runtime_tools_offered_every_round():
    access = AgentToolAccess(
        AGENT_TYPES["general"], agent_id="probe", depth=0, session_id=_sid(), ws_connected=False
    )
    runtime = {t["name"] for t in get_agent_runtime_tools("probe", 0)}
    offered = [t["name"] for t in access.offered()]
    assert runtime <= set(offered)
    assert len(offered) == len(set(offered)), "tool names must be unique"


def test_read_only_index_and_entitlement_hold_no_write_tool():
    access = AgentToolAccess(
        AGENT_TYPES["explore"], agent_id="probe", depth=0, session_id=_sid(), ws_connected=False
    )
    index = access.deferred_index()
    listed = [line[2:].split(" ", 1)[0] for line in index.splitlines() if line.startswith("- ")]
    assert listed, "a read-only agent still has deferred read tools to load"
    assert not any(_is_write_tool(n) for n in listed)
    assert not any(_is_write_tool(n) for n in access.scope.permitted)
    assert {t["name"] for t in access.offered()} <= access.scope.permitted


def test_read_only_agent_can_neither_delegate_nor_act_outside_the_workspace():
    """Each of these runs another agent with the full write set, starts or
    changes a scheduled job, saves something into the parent chat, or writes
    into another session, so none may be open to a read-only agent."""
    escapes = {
        "spawn_agent",
        "team_create",
        "skill_invoke",
        "cron_run",
        "cron_update",
        "create_artifact",
        "edit_artifact",
        "create_plan",
        "send_session_message",
    }
    for agent_type in ("explore", "plan"):
        access = AgentToolAccess(
            AGENT_TYPES[agent_type], agent_id="probe", depth=0, session_id=_sid(), ws_connected=True
        )
        assert escapes.isdisjoint(access.scope.permitted), agent_type
    general = AgentToolAccess(
        AGENT_TYPES["general"], agent_id="probe", depth=0, session_id=_sid(), ws_connected=True
    )
    assert escapes & _catalog_names() <= general.scope.permitted, "only read-only agents lose them"


# ── what an agent may run ───────────────────────────────────────────────────


def test_read_only_agent_write_call_is_refused_before_it_runs(monkeypatch):
    fake = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "ws_write_file", {"path": "x.txt", "content": "y"}),
                *msg_end(stop_reason="tool_use"),
            ],
            _done(),
        ]
    )
    dispatched = _patch_engine(monkeypatch, fake)
    asyncio.run(
        run_agent("look around", agent_type="explore", session_id=_sid(), model_id_override="m")
    )
    assert "ws_write_file" not in _names(fake.requests[0])
    assert dispatched == []
    assert _results(fake.requests[1])[0].startswith("[Refused]")


def test_session_summarizer_cannot_reach_memory_write_by_name(monkeypatch):
    """The field case: tool_search to find memory_write, then memory_write."""
    fake = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "tool_search", {"query": "select:memory_write"}),
                *tool_use_block(
                    "t2",
                    "memory_write",
                    {"filename": "session_memory.md", "content": "x"},
                    index=1,
                ),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("## Goals\n- ship"), *msg_end(stop_reason="end_turn")],
        ]
    )
    dispatched = _patch_engine(monkeypatch, fake)
    result = asyncio.run(
        run_agent(
            "summarise",
            agent_type="session_summarizer",
            session_id=_sid(),
            depth=1,
            model_id_override="m",
        )
    )
    assert dispatched == []
    assert all(r.startswith("[Refused]") for r in _results(fake.requests[1]))
    assert result.output.startswith("## Goals")


def test_tool_batch_refuses_names_outside_the_scope_and_only_when_scoped(monkeypatch):
    calls: list[str] = []

    async def route(name, tool_input, **kwargs):
        calls.append(name)
        return "ok", []

    monkeypatch.setattr(te, "route_tool", route)
    executor = ThreadPoolExecutor(max_workers=2)
    uses = [
        {"id": "a", "name": "ws_read_file", "input": {"path": "a.txt"}},
        {"id": "b", "name": "ws_write_file", "input": {"path": "b.txt", "content": "x"}},
    ]

    async def batch(scope):
        return await te.execute_tool_batch(
            uses,
            is_concurrent_safe=lambda n: False,
            loop=asyncio.get_running_loop(),
            executor=executor,
            transcript="",
            attachments=None,
            session_id="s1",
            session_denials={},
            model_id="claude",
            plan_mode=False,
            tool_scope=scope,
        )

    try:
        scope = ToolScope(permitted=frozenset({"ws_read_file"}), activation_key="agent:x")
        scoped = asyncio.run(batch(scope))
        assert calls == ["ws_read_file"]
        assert scoped[1].output.startswith("[Refused]")
        calls.clear()
        asyncio.run(batch(None))
        assert calls == ["ws_read_file", "ws_write_file"]
    finally:
        executor.shutdown(wait=False)


# ── tool_search inside an agent ─────────────────────────────────────────────


def test_agent_tool_search_loads_into_its_own_next_round_only(monkeypatch):
    name = _deferred_names()[0]
    sid = _sid()
    fake = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "tool_search", {"query": f"select:{name}"}),
                *msg_end(stop_reason="tool_use"),
            ],
            _done(),
        ]
    )
    _patch_engine(monkeypatch, fake)
    result = asyncio.run(
        run_agent("task", agent_type="general", session_id=sid, model_id_override="m")
    )
    assert name in json.dumps(fake.requests[0].get("system")), "the index lists what it can load"
    assert name not in _names(fake.requests[0])
    assert name in _names(fake.requests[1])
    assert name not in get_ordered(sid), "the parent chat's tools must not change"
    assert get_ordered(activation_key(result.agent_id)) == []


def test_read_only_agent_tool_search_cannot_load_a_write_tool(monkeypatch):
    write_name = sorted(WRITE_TOOLS & set(_deferred_names()))[0]
    fake = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "tool_search", {"query": f"select:{write_name}"}),
                *msg_end(stop_reason="tool_use"),
            ],
            _done(),
        ]
    )
    _patch_engine(monkeypatch, fake)
    asyncio.run(run_agent("look", agent_type="explore", session_id=_sid(), model_id_override="m"))
    assert json.loads(_results(fake.requests[1])[0])["matches"] == []
    assert write_name not in _names(fake.requests[1])


def test_an_entitled_tool_its_round_deferred_still_runs_by_name(monkeypatch):
    """As in chat and in Codex, the boundary is the entitlement, not the
    loading step: a tool the agent may run is not refused for being unloaded."""
    name = _deferred_names()[0]
    fake = FakeBedrockClient(
        [
            [msg_start(), *tool_use_block("t1", name, {}), *msg_end(stop_reason="tool_use")],
            _done(),
        ]
    )
    dispatched = _patch_engine(monkeypatch, fake)
    asyncio.run(run_agent("go", agent_type="general", session_id=_sid(), model_id_override="m"))
    assert name not in _names(fake.requests[0])
    assert dispatched == [name]
