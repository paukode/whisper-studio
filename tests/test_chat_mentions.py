"""The @ mentions of a message resolve once, when its turn starts
(server/chat/mentions.py): an MCP resource's text goes into the message, an
agent type gets its spawn hint, and a session's conversation is pulled in. A
mention that names nothing real stays as typed."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import mcp_catalog
from server.agent_tools import cross_session
from server.chat import mentions


@pytest.fixture
def resources(monkeypatch):
    reads: list[tuple[str, str]] = []

    async def read(server, uri):
        reads.append((server, uri))
        if uri == "issue://404":
            raise mcp_catalog.CatalogError("not found")
        return f"text of {uri}"

    monkeypatch.setattr(mcp_catalog, "server_by_mention", lambda n: {"github": "git hub"}.get(n))
    monkeypatch.setattr(mcp_catalog, "read_resource", read)
    monkeypatch.setattr(cross_session, "resolve_at_session_mentions", lambda q, sid: q)
    return reads


def _resolve(question, workspace=None):
    return asyncio.run(mentions.resolve_mentions(question, "sess-1", workspace))


def test_a_resource_goes_ahead_of_the_message(resources):
    question, agents = _resolve("Fix @github:issue://12, please.")

    assert question == (
        "[MCP resource issue://12 from git hub]\ntext of issue://12\n\nFix @github:issue://12, please."
    )
    assert resources == [("git hub", "issue://12")]
    assert agents == []


def test_a_resource_of_no_connected_server_or_one_that_fails_stays_as_typed(resources):
    question, _ = _resolve("See @jira:bug://1 and @github:issue://404")

    assert question == "See @jira:bug://1 and @github:issue://404"
    assert resources == [("git hub", "issue://404")]


def test_an_email_address_is_no_mention(resources):
    assert _resolve("Mail ana@github:issue://12")[0] == "Mail ana@github:issue://12"


def test_a_known_agent_is_named_once_and_an_unknown_one_is_not(resources, monkeypatch):
    from server.agents import config

    monkeypatch.setattr(
        config,
        "spawnable_agent_types",
        lambda ws: [
            {"name": "explore", "description": ""},
            {"name": "my-reviewer", "description": ""},
        ],
    )

    question, agents = _resolve(
        "@agent-explore and @agent-my-reviewer, then @agent-explore. @agent-nobody"
    )

    assert agents == ["explore", "my-reviewer"]
    assert question.startswith("@agent-explore and")


def test_the_agent_hint_asks_for_spawn_agent_with_that_type():
    assert "agent_type `explore`" in mentions.agent_hint(["explore"])
    assert "`explore`, `plan`" in mentions.agent_hint(["explore", "plan"])


def test_a_session_mention_never_takes_a_resource_or_its_colon():
    marker = cross_session._AT_SESSION_MARKER

    assert [m.group(1) for m in marker.finditer("ask @notes, then @github:issue://1")] == ["notes"]


def test_the_agent_types_route_lists_custom_types_and_hides_internal_ones(tmp_path, monkeypatch):
    from server.agents import custom_config
    from server.agents.routes import router

    user_dir = tmp_path / "agents"
    user_dir.mkdir()
    (user_dir / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Reviews a diff\n---\nYou review diffs."
    )
    monkeypatch.setattr(custom_config, "_user_agents_dir", lambda: str(user_dir))
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    app = FastAPI()
    app.include_router(router)

    types = {
        t["name"]: t["description"]
        for t in TestClient(app).get("/api/agents/types").json()["types"]
    }

    assert types["reviewer"] == "Reviews a diff"
    assert types["explore"] == "Read-only, for code search and analysis"
    assert "memory_extractor" not in types and "session_summarizer" not in types
