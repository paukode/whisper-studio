"""An agent runs only what it is entitled to, and is offered what it needs.

Two layers (server/agents/tool_access.py): the tools array decides what the
model is shown, and the tool executor refuses every name outside the run's
entitlement, whatever the model emits. Field cases: a session summary run
offered four tools ran tool_search, memory_list and memory_write by name and
saved a long-term memory file, and the memory agents were never offered the
memory tools their whitelist names while chat deferred them.

These run the REAL run_agent, router and tool catalog; only the model is
scripted, and the end-to-end cases write into a real temporary workspace.
"""

import asyncio
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

import server.tool_executor as te
from server.agents import journal as journal_mod
from server.agents.config import AGENT_TYPES, AgentConfig, _is_write_tool
from server.agents.runtime import run_agent
from server.agents.tool_access import AgentToolAccess, ToolScope, activation_key
from server.agents.tools import get_agent_runtime_tools
from server.chat.tool_activation import get_ordered
from server.chat.tool_partition import core_names
from server.chat.tool_pool import assemble_full_catalog
from server.tasks import registry, shell
from server.tool_router import route_tool
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block

INDEX_HEADING = "## Additional tools (not loaded)"
WHITELISTED = sorted(name for name, cfg in AGENT_TYPES.items() if cfg.allowed_tools is not None)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from server.approval.bootstrap import register_defaults

    register_defaults()  # the unattended gate executes only registered actions
    monkeypatch.setattr(journal_mod, "storage_root", lambda: str(tmp_path / "storage"))
    monkeypatch.setattr(registry, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))
    monkeypatch.setattr("server.costs.budget.check_budget", lambda session_id: None)


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _script(monkeypatch, *calls) -> FakeBedrockClient:
    """One round per (tool name, input) call, then a closing text round."""
    rounds = [
        [msg_start(), *tool_use_block(f"t{i}", name, args), *msg_end("tool_use")]
        for i, (name, args) in enumerate(calls)
    ]
    rounds.append([msg_start(), *text_block("done"), *msg_end()])
    fake = FakeBedrockClient(rounds)
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake)
    return fake


def _run(**kw):
    kw.setdefault("session_id", f"s-{uuid.uuid4().hex[:8]}")
    return asyncio.run(run_agent("look around", model_id_override="test-model", **kw))


def _offered(fake, round_num: int = 0) -> set[str]:
    return {t["name"] for t in fake.requests[round_num].get("tools", [])}


def _result(fake, call: int) -> str:
    """The tool_result the model read back for its call-th call."""
    return fake.requests[call + 1]["messages"][-1]["content"][0]["content"]


def _system(fake) -> str:
    system = fake.requests[0].get("system") or ""
    return system if isinstance(system, str) else "".join(b.get("text", "") for b in system)


def _index_entries(fake) -> list[str]:
    """Tool names the deferred index in the agent's system prompt lists."""
    if INDEX_HEADING not in _system(fake):
        return []
    names: list[str] = []
    for line in _system(fake).split(INDEX_HEADING, 1)[1].splitlines():
        if line.startswith("- "):
            names.append(line[2:].split(" ", 1)[0])
        elif names:
            break
    return names


def _journal_statuses(agent_id: str) -> list[str]:
    path = os.path.join(journal_mod.load(agent_id)["dir"], "events.jsonl")
    events = [json.loads(line) for line in open(path)]
    return [e["status"] for e in events if e["phase"] == "tool_result"]


def _access(agent_type: str, **kw) -> AgentToolAccess:
    return AgentToolAccess(
        AGENT_TYPES[agent_type],
        agent_id="probe",
        depth=kw.get("depth", 0),
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        ws_connected=kw.get("ws_connected", True),
    )


def _deferred_names() -> list[str]:
    catalog = {t["name"] for t in assemble_full_catalog(plan_mode=False, ws_connected=True)}
    runtime = {t["name"] for t in get_agent_runtime_tools("probe", 0)}
    return sorted(catalog - core_names() - runtime)


# ── what an agent is offered ────────────────────────────────────────────────


@pytest.mark.parametrize("agent_type", WHITELISTED)
def test_whitelisted_agent_is_offered_its_whole_list_and_nothing_to_search(agent_type):
    access = _access(agent_type, depth=1)
    offered = {t["name"] for t in access.offered()}
    assert offered == access.scope.permitted
    assert offered <= AGENT_TYPES[agent_type].allowed_tools
    assert access.deferred_index() == ""


def test_a_whitelisted_tool_chat_defers_is_still_offered(monkeypatch):
    """The case that broke: a whitelist that names a tool chat defers (the
    memory tools were) and leaves out tool_search had no way to reach it."""
    deferred = _deferred_names()[0]
    fake = _script(monkeypatch)
    _run(config=AgentConfig(agent_type="probe", allowed_tools=frozenset({deferred, "ws_grep"})))
    assert deferred in _offered(fake)
    assert "tool_search" not in _offered(fake)
    assert INDEX_HEADING not in _system(fake)


def test_spawnable_agent_keeps_its_runtime_tools_offered_every_round():
    offered = [t["name"] for t in _access("general").offered()]
    assert {t["name"] for t in get_agent_runtime_tools("probe", 0)} <= set(offered)
    assert len(offered) == len(set(offered)), "tool names must be unique"


def test_read_only_index_and_entitlement_hold_no_write_tool():
    access = _access("explore")
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
        assert escapes.isdisjoint(_access(agent_type).scope.permitted), agent_type
    catalog = {t["name"] for t in assemble_full_catalog(plan_mode=False, ws_connected=True)}
    assert escapes & catalog <= _access("general").scope.permitted, (
        "only read-only agents lose them"
    )


# ── what an agent may run, end to end ───────────────────────────────────────


def test_read_only_agent_cannot_write_or_run_by_naming_the_tool(monkeypatch, workspace):
    calls = [
        ("ws_create_file", {"path": "made.txt", "content": "x"}),
        ("ws_run_command", {"command": "echo ran > ran.txt"}),
    ]
    fake = _script(monkeypatch, *calls)
    result = _run(agent_type="explore", workspace_path=str(workspace))

    assert not _offered(fake) & {name for name, _ in calls}, "named, never offered"
    for i, (name, _) in enumerate(calls):
        assert _result(fake, i).startswith(f"[Refused] '{name}'")
    assert list(workspace.iterdir()) == []
    assert result.status == "completed"
    # The team card reads these: a refusal shows as a failed call.
    assert _journal_statuses(result.agent_id) == ["error", "error"]


def test_the_same_call_from_a_writing_agent_does_write(monkeypatch, workspace):
    """The control that keeps the test above honest: in this harness the
    call writes the file when the agent may make it."""
    fake = _script(monkeypatch, ("ws_create_file", {"path": "made.txt", "content": "x"}))
    _run(agent_type="general", workspace_path=str(workspace))

    assert not _result(fake, 0).startswith("[Refused]")
    assert (workspace / "made.txt").read_text() == "x"


def test_whitelisted_agent_runs_its_list_and_nothing_else(monkeypatch):
    """The field run replayed: the extractor's first call was tool_search,
    which it had only read about in an index of tools it may not use."""
    fake = _script(
        monkeypatch,
        ("tool_search", {"query": "memory_write memory_list memory_read"}),
        ("ws_write_file", {"path": "notes.md", "content": "x"}),
        ("skill_list", {}),
        ("memory_list", {}),
    )
    _run(agent_type="memory_extractor", cost_source="memory")

    assert {"memory_list", "memory_write"} <= _offered(fake)
    assert "tool_search" not in _offered(fake)
    assert INDEX_HEADING not in _system(fake)
    assert _result(fake, 0).startswith("[Refused] 'tool_search'")
    assert _result(fake, 1).startswith("[Refused] 'ws_write_file'")
    assert not _result(fake, 2).startswith("[Refused]")
    assert not _result(fake, 3).startswith("[Refused]")


def test_session_summarizer_cannot_reach_memory_write_by_name(monkeypatch):
    """The field case: tool_search to find memory_write, then memory_write."""
    fake = _script(
        monkeypatch,
        ("tool_search", {"query": "select:memory_write"}),
        ("memory_write", {"filename": "session_memory.md", "content": "x"}),
    )
    _run(agent_type="session_summarizer", depth=1, cost_source="memory")

    assert _result(fake, 0).startswith("[Refused] 'tool_search'")
    assert _result(fake, 1).startswith("[Refused] 'memory_write'")


def test_general_agent_runs_an_unloaded_tool_but_nothing_outside_its_catalog(monkeypatch):
    """As in chat and in Codex, the boundary is the entitlement, not the
    loading step: a catalog tool its round deferred still runs by name. A
    tool outside the agent's catalog does not (workflow_run exists only in an
    ultracode chat's catalog, so an agent cannot start a workflow)."""
    assert "config_get" in _deferred_names()
    fake = _script(monkeypatch, ("config_get", {}), ("workflow_run", {"script": "x"}))
    _run(agent_type="general")

    assert "config_get" not in _offered(fake)
    assert not _result(fake, 0).startswith("[Refused]")
    assert _result(fake, 1).startswith("[Refused] 'workflow_run'")


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


def test_read_only_agent_is_shown_and_finds_only_tools_it_may_run(monkeypatch, workspace):
    wanted = ["ws_delete_file", "run_python", "memory_write", "git_log"]
    fake = _script(monkeypatch, ("tool_search", {"query": "select:" + ",".join(wanted)}))
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    result = _run(agent_type="plan", session_id=session_id, workspace_path=str(workspace))

    index = _index_entries(fake)
    assert "git_log" in index
    assert [n for n in index if _is_write_tool(n)] == []
    found = [m["name"] for m in json.loads(_result(fake, 0))["matches"]]
    assert found == ["git_log"]
    # Loaded into the agent's own set: offered from its next round, while
    # the parent chat's tools do not change and the set ends with the run.
    assert "git_log" not in _offered(fake, 0) and "git_log" in _offered(fake, 1)
    assert get_ordered(session_id) == []
    assert get_ordered(activation_key(result.agent_id)) == []


def test_tool_search_in_an_agent_never_finds_the_workflow_tools():
    """An ultracode parent's effort reaches its agents, and tool_search builds
    the ultracode catalog from it; only the scope keeps workflow_run (a
    workflow launched from inside an agent) out of an agent's reach."""

    async def search(scope):
        output, _ = await route_tool(
            "tool_search",
            {"query": "select:workflow_run,memory_list", "activate": False},
            loop=asyncio.get_running_loop(),
            executor=None,
            transcript="",
            attachments=None,
            session_id="",
            model_id="test-model",
            tool_use_id="t0",
            effort_label="ultracode",
            tool_scope=scope,
        )
        return [m["name"] for m in json.loads(output)["matches"]]

    assert asyncio.run(search(None)) == ["workflow_run", "memory_list"]
    scope = ToolScope(permitted=frozenset({"memory_list"}), activation_key="agent:x")
    assert asyncio.run(search(scope)) == ["memory_list"]
