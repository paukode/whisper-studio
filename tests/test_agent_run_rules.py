"""Rules an agent run keeps beyond its own type's tool list.

- An agent started from a plan-mode turn runs read-only, and the flag lasts
  only as long as the call that started it.
- A read-only agent runs an MCP tool only when it is marked read-only.
- Refused calls end a run: REFUSAL_STREAK_LIMIT in a row make the next round
  the last, with no tools, instead of spending the whole budget.
- The memory tools are always offered in chat turns only.
- The rules are fixed for a run; the catalog they apply to is read each round.

The end-to-end cases run the REAL run_agent, router and catalog with only the
model scripted (tests/golden_harness.py), as tests/test_agent_tool_access.py.
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

import server.tool_executor as te
from server.agents import journal as journal_mod
from server.agents.config import AGENT_TYPES, _is_write_tool, filter_tools_for_agent
from server.agents.runtime import run_agent
from server.agents.tool_access import (
    REFUSAL_STREAK_LIMIT,
    AgentToolAccess,
    ToolScope,
    plan_mode_scope,
    started_in_plan_mode,
)
from server.chat.tool_partition import CHAT_CORE_TOOLS, core_names, partition_pool
from server.tasks import registry, shell
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from server.approval.bootstrap import register_defaults

    register_defaults()
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


def _result(fake, call: int) -> str:
    return fake.requests[call + 1]["messages"][-1]["content"][0]["content"]


def _access(agent_type: str) -> AgentToolAccess:
    return AgentToolAccess(
        AGENT_TYPES[agent_type],
        agent_id="probe",
        depth=0,
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        ws_connected=True,
    )


# ── plan mode reaches agents ────────────────────────────────────────────────


def test_an_agent_started_in_plan_mode_cannot_write(monkeypatch, workspace):
    create = ("ws_create_file", {"path": "made.txt", "content": "x"})
    fake = _script(monkeypatch, create)
    token = plan_mode_scope.set(True)
    try:
        _run(agent_type="general", workspace_path=str(workspace))
    finally:
        plan_mode_scope.reset(token)
    assert _result(fake, 0).startswith("[Refused] 'ws_create_file'")
    assert list(workspace.iterdir()) == []

    # The same agent out of plan mode writes.
    _script(monkeypatch, create)
    _run(agent_type="general", workspace_path=str(workspace))
    assert (workspace / "made.txt").read_text() == "x"


def test_the_executor_hands_plan_mode_to_the_call_and_takes_it_back(monkeypatch):
    seen: list[bool] = []

    async def route(name, tool_input, **kwargs):
        seen.append(started_in_plan_mode())
        return "ok", []

    monkeypatch.setattr(te, "route_tool", route)
    executor = ThreadPoolExecutor(max_workers=1)

    async def batch(plan_mode: bool):
        await te.execute_tool_batch(
            [{"id": "a", "name": "ws_read_file", "input": {"path": "a.txt"}}],
            is_concurrent_safe=lambda n: False,
            loop=asyncio.get_running_loop(),
            executor=executor,
            transcript="",
            attachments=None,
            session_id="s1",
            session_denials={},
            model_id="claude",
            plan_mode=plan_mode,
        )
        return started_in_plan_mode()

    try:
        assert asyncio.run(batch(True)) is False, "the flag never outlives the call"
        assert asyncio.run(batch(False)) is False
    finally:
        executor.shutdown(wait=False)
    assert seen == [True, False]


# ── MCP tools for read-only agents ──────────────────────────────────────────


class _Hint:
    def __init__(self, read_only: bool):
        self.readOnlyHint = read_only


class _McpTool:
    def __init__(self, name: str, read_only: bool | None = None):
        self.name = name
        self.annotations = _Hint(read_only) if read_only is not None else None


def _fake_mcp(monkeypatch, tools: list[_McpTool], config: dict) -> None:
    from server.mcp import mcp_manager

    monkeypatch.setattr(
        mcp_manager,
        "_tools",
        {
            f"mcp__srv__{t.name}": {"server_name": "srv", "original_name": t.name, "mcp_tool": t}
            for t in tools
        },
    )
    monkeypatch.setattr(mcp_manager, "load_config", lambda: config)


@pytest.mark.parametrize(
    ("hint", "config", "is_write"),
    [
        (None, {}, True),  # unmarked: kept from read-only agents
        (True, {}, False),  # the server marks it read-only
        (False, {}, True),
        (None, {"srv": {"read_only_tools": ["lookup"]}}, False),
        (None, {"srv": {"read_only": True}}, False),
        (True, {"srv": {"read_only": False}}, True),  # the user stops trusting hints
        (True, {"srv": {"read_only": False, "read_only_tools": ["lookup"]}}, False),
    ],
)
def test_an_mcp_tool_is_a_read_only_when_it_is_marked_one(monkeypatch, hint, config, is_write):
    _fake_mcp(monkeypatch, [_McpTool("lookup", hint)], config)
    assert _is_write_tool("mcp__srv__lookup") is is_write


def test_a_read_only_agent_is_offered_only_the_marked_mcp_tools(monkeypatch):
    _fake_mcp(monkeypatch, [_McpTool("lookup", True), _McpTool("create_issue")], {})
    pool = [{"name": "mcp__srv__lookup"}, {"name": "mcp__srv__create_issue"}]
    kept = {t["name"] for t in filter_tools_for_agent(pool, AGENT_TYPES["explore"])}
    assert kept == {"mcp__srv__lookup"}
    assert {t["name"] for t in filter_tools_for_agent(pool, AGENT_TYPES["general"])} == {
        t["name"] for t in pool
    }


# ── refused calls end the run ───────────────────────────────────────────────


def test_refused_calls_in_a_row_set_the_budget_to_finish():
    budget: dict = {}
    scope = ToolScope(permitted=frozenset(), activation_key="agent:x", budget=budget)
    for _ in range(REFUSAL_STREAK_LIMIT - 1):
        scope.refused()
    scope.ran()  # a call that runs starts the count again
    for _ in range(REFUSAL_STREAK_LIMIT - 1):
        scope.refused()
    assert not budget.get("finish")
    scope.refused()
    assert budget.get("finish") is True


def test_an_agent_that_keeps_calling_what_it_may_not_run_answers_early(monkeypatch, workspace):
    writes = [("ws_create_file", {"path": f"f{i}.txt", "content": "x"}) for i in range(8)]
    fake = _script(monkeypatch, *writes)
    result = _run(agent_type="explore", workspace_path=str(workspace))

    # The refused rounds, then one last round with no tools to answer in.
    assert len(fake.requests) == REFUSAL_STREAK_LIMIT + 1
    assert "tools" not in fake.requests[-1]
    assert list(workspace.iterdir()) == []
    assert result.status == "completed"


def test_the_loop_guards_refusals_count_too(monkeypatch, workspace):
    """The field case: a memory tidy spent 25 rounds on calls the loop guard
    kept refusing. An agent repeating one permitted call stops early too."""
    (workspace / "notes.txt").write_text("the notes")
    reads = [("ws_read_file", {"path": "notes.txt"})] * 10
    fake = _script(monkeypatch, *reads)
    _run(agent_type="explore", workspace_path=str(workspace))
    refused = [i for i in range(len(fake.requests) - 1) if "same" in _result(fake, i).lower()]
    assert refused, "the loop guard refused the repeats"
    assert len(fake.requests) < len(reads) + 1
    assert "tools" not in fake.requests[-1]


# ── memory tools are core in chat only ──────────────────────────────────────


def test_the_memory_tools_are_core_in_chat_turns_only():
    assert CHAT_CORE_TOOLS <= core_names(chat=True)
    assert CHAT_CORE_TOOLS.isdisjoint(core_names(chat=False))
    catalog = [{"name": n} for n in sorted(CHAT_CORE_TOOLS | {"ws_read_file"})]
    chat, _, _ = partition_pool(catalog, [], chat=True)
    other, deferred, _ = partition_pool(catalog, [], chat=False)
    assert CHAT_CORE_TOOLS <= {t["name"] for t in chat}
    assert CHAT_CORE_TOOLS.isdisjoint({t["name"] for t in other})
    assert CHAT_CORE_TOOLS <= {t["name"] for t in deferred}


def test_scheduled_and_voice_turns_defer_the_memory_tools(monkeypatch):
    from server.chat import tool_pool

    catalog = [{"name": n} for n in sorted(CHAT_CORE_TOOLS | {"ws_read_file", "tool_search"})]
    monkeypatch.setattr(tool_pool, "assemble_full_catalog", lambda **kw: list(catalog))
    monkeypatch.setattr(
        "server.infrastructure.feature_flags.is_enabled", lambda name, *a, **k: True
    )
    chat, _, _ = tool_pool.assemble_partitioned_pool(session_id="s-chat")
    other, deferred, _ = tool_pool.assemble_partitioned_pool(session_id="s-cron", chat=False)
    assert CHAT_CORE_TOOLS <= {t["name"] for t in chat}
    assert CHAT_CORE_TOOLS.isdisjoint({t["name"] for t in other})
    assert CHAT_CORE_TOOLS <= {t["name"] for t in deferred}


def test_an_agent_can_still_load_the_memory_tools_it_is_not_offered(monkeypatch):
    catalog = [{"name": "ws_read_file"}, {"name": "tool_search"}, {"name": "memory_write"}]
    monkeypatch.setattr("server.chat.tool_pool.assemble_full_catalog", lambda **kw: catalog)
    access = _access("general")
    assert "memory_write" not in {t["name"] for t in access.offered()}
    assert "memory_write" in access.scope.permitted
    assert "memory_write" in access.deferred_index()


# ── the catalog is read each round ──────────────────────────────────────────


def test_an_mcp_server_that_connects_mid_run_is_available_next_round(monkeypatch):
    catalog = [{"name": "ws_read_file"}, {"name": "tool_search"}]
    monkeypatch.setattr("server.chat.tool_pool.assemble_full_catalog", lambda **kw: list(catalog))
    _fake_mcp(monkeypatch, [_McpTool("lookup")], {})
    general, explore = _access("general"), _access("explore")
    assert "mcp__srv__lookup" not in general.scope.permitted

    catalog.append({"name": "mcp__srv__lookup"})
    general.offered()
    explore.offered()
    assert "mcp__srv__lookup" in general.scope.permitted
    assert "mcp__srv__lookup" not in explore.scope.permitted, "the same rules apply to it"
