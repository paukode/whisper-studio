"""MCP prompts run as /server:prompt and resources are mentioned as @server:uri.

A connected server's prompts and resources reach the composer through
GET /api/mcp/servers. POST /api/mcp/prompt runs one prompt and stores its text
as the message's attachment, so later turns still see it.
"""

import asyncio

import mcp.types as types
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import attachments, mcp_catalog, mcp_routes
from server.mcp import mcp_manager


class _Session:
    """A connected server with one prompt and one resource."""

    def __init__(self, messages=None):
        self.prompt_calls: list[tuple[str, dict]] = []
        self.messages = messages or [
            types.PromptMessage(
                role="user", content=types.TextContent(type="text", text="Review PR")
            )
        ]

    async def get_prompt(self, name, arguments=None):
        self.prompt_calls.append((name, arguments))
        return types.GetPromptResult(messages=self.messages)

    async def read_resource(self, uri):
        if str(uri) != "issue://12":
            raise RuntimeError("no such issue")
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri="issue://12", text="The login button fails.")]
        )


def _prompt(name="pr_review", arguments=None):
    return types.Prompt(
        name=name,
        description="Review a pull request",
        arguments=arguments
        if arguments is not None
        else [
            types.PromptArgument(name="number", required=True),
            types.PromptArgument(name="focus", required=False),
        ],
    )


@pytest.fixture
def server(monkeypatch):
    session = _Session()
    sessions = {
        "git hub": {
            "status": "connected",
            "session": session,
            "config": {},
            "tools": {},
            "prompts": {"pr_review": _prompt()},
            "resources": {"issue://12": types.Resource(uri="issue://12", name="Issue 12")},
        },
        "offline": {"status": "error", "config": {}, "tools": {}},
    }
    monkeypatch.setattr(mcp_manager, "_sessions", sessions)
    return session


def test_a_prompt_is_listed_with_its_command_and_arguments(server):
    [prompt] = mcp_catalog.server_prompts("git hub")

    assert prompt["command"] == "git_hub:pr_review"
    assert [(a["name"], a["required"]) for a in prompt["arguments"]] == [
        ("number", True),
        ("focus", False),
    ]
    assert mcp_catalog.server_prompts("offline") == []


def test_the_words_fill_the_arguments_in_order_and_the_last_takes_the_rest(server):
    filename, text = asyncio.run(
        mcp_catalog.render_prompt("git_hub:pr_review", '456 "the auth" changes')
    )

    assert server.prompt_calls == [("pr_review", {"number": "456", "focus": "the auth changes"})]
    assert filename == "/git_hub:pr_review (MCP prompt)"
    assert text == "[MCP prompt /git_hub:pr_review from git hub]\n\nReview PR"


def test_a_missing_required_argument_is_named(server):
    with pytest.raises(mcp_catalog.CatalogError, match="needs: number"):
        asyncio.run(mcp_catalog.render_prompt("git_hub:pr_review", ""))
    assert server.prompt_calls == []


def test_a_prompt_of_several_messages_keeps_who_said_what(server):
    server.messages = [
        types.PromptMessage(role="user", content=types.TextContent(type="text", text="Hi")),
        types.PromptMessage(role="assistant", content=types.TextContent(type="text", text="Ready")),
    ]

    _, text = asyncio.run(mcp_catalog.render_prompt("git_hub:pr_review", "1"))

    assert text.endswith("User: Hi\n\nAssistant: Ready")


def test_an_unknown_server_or_prompt_says_so(server):
    with pytest.raises(mcp_catalog.CatalogError, match="No connected MCP server"):
        asyncio.run(mcp_catalog.render_prompt("offline:pr_review", "1"))
    with pytest.raises(mcp_catalog.CatalogError, match="no prompt named nope"):
        asyncio.run(mcp_catalog.render_prompt("git_hub:nope", "1"))


def test_a_resource_reads_as_text_and_a_failure_raises(server):
    assert (
        asyncio.run(mcp_catalog.read_resource("git hub", "issue://12")) == "The login button fails."
    )
    with pytest.raises(mcp_catalog.CatalogError, match="could not read issue://99"):
        asyncio.run(mcp_catalog.read_resource("git hub", "issue://99"))
    assert mcp_catalog.all_resources() == [
        {
            "uri": "issue://12",
            "mention": "git_hub:issue://12",
            "name": "Issue 12",
            "description": "",
            "server": "git hub",
        }
    ]


@pytest.fixture
def client(server, monkeypatch):
    monkeypatch.setattr(
        mcp_manager,
        "load_config",
        lambda: {"git hub": {"command": "gh-mcp"}, "offline": {"command": "x"}},
    )
    app = FastAPI()
    app.include_router(mcp_routes.router)
    return TestClient(app)


def test_the_server_list_carries_prompts_and_resources(client):
    servers = client.get("/api/mcp/servers").json()["servers"]

    assert servers["git hub"]["prompts"][0]["command"] == "git_hub:pr_review"
    assert servers["git hub"]["resources"][0]["uri"] == "issue://12"
    assert servers["offline"]["prompts"] == [] and servers["offline"]["resources"] == []


def test_running_a_prompt_stores_its_text_as_an_attachment(client, monkeypatch):
    stored: dict[str, dict] = {}

    async def _store(aid, record):
        stored[aid] = record

    monkeypatch.setattr(attachments, "_store", _store)

    r = client.post("/api/mcp/prompt", json={"command": "git_hub:pr_review", "arguments": "7"})

    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "/git_hub:pr_review (MCP prompt)"
    assert stored[body["id"]]["text"].endswith("Review PR")


def test_a_prompt_that_cannot_run_is_a_400_with_the_reason(client):
    r = client.post("/api/mcp/prompt", json={"command": "git_hub:pr_review", "arguments": ""})

    assert r.status_code == 400
    assert r.json()["error"] == "/git_hub:pr_review needs: number."
