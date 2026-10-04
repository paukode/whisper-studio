"""A read-only agent's children are read-only too.

Agents approve their own [WS_APPROVAL] gates, so a read-only agent that can
start a writing one is not read-only (the SECURITY INVARIANT above WRITE_TOOLS
in server/agents/config.py). Two layers hold it:

- spawn_agent, team_create and skill_invoke are write tools, so the tool
  executor refuses them from a read-only agent at execution.
- Whatever a read-only agent still starts or resumes runs read-only
  (server.agents.tool_access.read_only_scope, applied in run_agent).

These run the REAL run_agent, router, delegation tools and catalog, as
tests/test_agent_tool_access.py does. Only the model is scripted, one script
per agent, and the writes land in a real temporary workspace.
"""

import asyncio
import json
import uuid

import pytest

import server.agents.config as agent_config
from server.agents import journal as journal_mod
from server.agents.runtime import run_agent
from server.tasks import registry, shell
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block

CHILD_TASK = "make the file [child]"
DELEGATIONS = {
    "spawn_agent": {"task": CHILD_TASK, "agent_type": "general"},
    "skill_invoke": {"skill_name": "notes", "input": CHILD_TASK},
    "team_create": {
        "team_name": "makers",
        "agents": [{"name": "maker", "task": CHILD_TASK, "agent_type": "general"}],
    },
}
CREATE = ("ws_create_file", {"path": "made.txt", "content": "x"})


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


class _Agents:
    """The Bedrock client for several agents: each gets its own scripted
    rounds, picked by the [marker] in its first message (its task), so a
    parent and the agents it starts share one client."""

    def __init__(self, **scripts: list[list[dict]]):
        self.fakes = {marker: FakeBedrockClient(rounds) for marker, rounds in scripts.items()}

    def invoke_model_with_response_stream(self, modelId, contentType, accept, body):
        first = json.dumps(json.loads(body)["messages"][0])
        for marker, fake in self.fakes.items():
            if f"[{marker}]" in first:
                return fake.invoke_model_with_response_stream(modelId, contentType, accept, body)
        raise RuntimeError(f"an agent no script names ran: {first[:200]}")


def _calls(*calls, closing: str = "done") -> list[list[dict]]:
    """One round per (tool name, input) call, then a closing text round."""
    rounds = [
        [msg_start(), *tool_use_block(f"t{i}", name, args), *msg_end("tool_use")]
        for i, (name, args) in enumerate(calls)
    ]
    rounds.append([msg_start(), *text_block(closing), *msg_end()])
    return rounds


def _model(monkeypatch, **scripts) -> _Agents:
    model = _Agents(**scripts)
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: model)
    return model


def _run(agent_type: str, task: str, **kw):
    kw.setdefault("session_id", f"s-{uuid.uuid4().hex[:8]}")
    return run_agent(task, agent_type=agent_type, model_id_override="test-model", **kw)


def _offered(fake, request: int = 0) -> set[str]:
    return {t["name"] for t in fake.requests[request].get("tools", [])}


def _result(fake, request: int) -> str:
    """The tool result the agent read back in its request-th request."""
    return fake.requests[request]["messages"][-1]["content"][0]["content"]


def _delegate(monkeypatch, workspace, parent_type: str, tool: str) -> _Agents:
    """A parent of parent_type calls the delegation tool; the agent it would
    start creates a file in the workspace."""
    model = _model(monkeypatch, parent=_calls((tool, DELEGATIONS[tool])), child=_calls(CREATE))
    asyncio.run(_run(parent_type, "look around [parent]", workspace_path=str(workspace)))
    return model


@pytest.mark.parametrize("tool", sorted(DELEGATIONS))
def test_a_read_only_agent_cannot_start_another_agent(monkeypatch, workspace, tool):
    model = _delegate(monkeypatch, workspace, "explore", tool)

    assert _result(model.fakes["parent"], 1).startswith(f"[Refused] '{tool}'")
    assert model.fakes["child"].requests == [], "no agent started"
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("tool", sorted(DELEGATIONS))
def test_a_writing_agent_starts_one_that_writes(monkeypatch, workspace, tool):
    """The control that keeps this file honest: through each delegation tool
    the child makes the file when nothing holds it to reads."""
    model = _delegate(monkeypatch, workspace, "general", tool)

    assert not _result(model.fakes["child"], 1).startswith("[Refused]")
    assert (workspace / "made.txt").read_text() == "x"


@pytest.mark.parametrize("tool", sorted(DELEGATIONS))
def test_what_a_read_only_agent_starts_may_only_read(monkeypatch, workspace, tool):
    """The second layer alone: with the delegation tool open to a read-only
    agent, the agent it starts still only reads."""
    monkeypatch.setattr(agent_config, "WRITE_TOOLS", agent_config.WRITE_TOOLS - {tool})
    model = _delegate(monkeypatch, workspace, "explore", tool)
    child = model.fakes["child"]

    assert not _result(model.fakes["parent"], 1).startswith("[Refused]")
    assert child.requests, "the child ran"
    assert CREATE[0] not in _offered(child)
    assert _result(child, 1).startswith(f"[Refused] '{CREATE[0]}'")
    assert list(workspace.iterdir()) == []
