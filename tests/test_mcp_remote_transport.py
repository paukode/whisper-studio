"""Remote/HTTP MCP transport.

A server config with a `url` key uses `streamable_http_client` instead of
`StdioServerParameters`/`stdio_client`. An optional `bearer_token_env_var`
names an environment variable read at connect time; the actual token value
is never persisted into mcp_servers.json — only the env var name is.
"""

import asyncio
import json
import os
import tempfile
from types import SimpleNamespace

import mcp
import mcp.client.stdio
import mcp.client.streamable_http
import pytest

from server import mcp as mcp_module
from server import mcp_routes
from server.mcp import MCPManager
from server.mcp_routes import mcp_add_server


@pytest.fixture
def isolated_mcp(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mcp_servers.json")
        monkeypatch.setattr(mcp_module, "MCP_CONFIG_PATH", path)
        yield path


class _FakeTool:
    def __init__(self, name):
        self.name = name
        self.description = "desc"
        self.input_schema = {"type": "object"}


class _FakeToolsResult:
    def __init__(self):
        self.tools = [_FakeTool("ping")]
        self.next_cursor = None


class _FakeSession:
    def __init__(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        # A server that does not advertise the resources capability.
        return SimpleNamespace(capabilities=SimpleNamespace(resources=None))

    async def list_tools(self, params=None):
        return _FakeToolsResult()

    async def list_resources(self, params=None):
        raise RuntimeError("no resources")


class _FakeStreamableCtx:
    """Records the url and the HTTP client streamable_http_client was invoked
    with, and yields the pair the real transport does: (read, write)."""

    def __init__(self, record, url, http_client):
        record["url"] = url
        record["headers"] = http_client.headers
        record["http_client"] = http_client

    async def __aenter__(self):
        return ("read-stream", "write-stream")

    async def __aexit__(self, *exc):
        return False


def _install_fakes(monkeypatch, record):
    monkeypatch.setattr(
        mcp,
        "ClientSession",
        lambda r, w, elicitation_callback=None, message_handler=None: _FakeSession(),
        raising=False,
    )
    monkeypatch.setattr(
        mcp.client.streamable_http,
        "streamable_http_client",
        lambda url, http_client=None: _FakeStreamableCtx(record, url, http_client),
        raising=False,
    )


def test_url_config_uses_streamable_http_transport(monkeypatch):
    record: dict = {}
    _install_fakes(monkeypatch, record)

    # If the stdio path were used instead, this would raise loudly rather
    # than silently succeeding via the wrong transport.
    def _boom(*a, **k):
        raise AssertionError("stdio_client must not be used for a url-configured server")

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", _boom, raising=False)

    mgr = MCPManager()

    async def scenario():
        await mgr.start_server("remote-srv", {"url": "https://example.com/mcp"})
        assert mgr._sessions["remote-srv"]["status"] == "connected"
        assert "mcp__remote-srv__ping" in mgr._tools
        assert record["url"] == "https://example.com/mcp"
        assert "authorization" not in record["headers"]
        # Redirects stay with the transport, which keeps them on the origin.
        assert record["http_client"].follow_redirects is False
        await mgr.stop_server("remote-srv")
        assert record["http_client"].is_closed

    asyncio.run(scenario())


def test_command_config_still_uses_stdio_transport(monkeypatch):
    """Sanity check the branch: a `command`-only config is unaffected by the
    new url path and still goes through stdio_client."""
    record: dict = {}
    _install_fakes(monkeypatch, record)

    class _FakeStdioCtx:
        async def __aenter__(self):
            record["stdio_used"] = True
            return ("read", "write")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        mcp.client.stdio, "stdio_client", lambda params: _FakeStdioCtx(), raising=False
    )
    monkeypatch.setattr(
        MCPManager, "_resolve_command", staticmethod(lambda command, path=None: command)
    )

    mgr = MCPManager()

    async def scenario():
        await mgr.start_server("local-srv", {"command": "fake"})
        assert mgr._sessions["local-srv"]["status"] == "connected"
        assert record.get("stdio_used") is True
        assert "url" not in record

    asyncio.run(scenario())


def test_bearer_token_read_from_env_and_sent_as_auth_header(monkeypatch):
    record: dict = {}
    _install_fakes(monkeypatch, record)
    monkeypatch.setenv("MY_MCP_TOKEN", "s3cr3t-value")

    mgr = MCPManager()

    async def scenario():
        await mgr.start_server(
            "remote-srv",
            {"url": "https://example.com/mcp", "bearer_token_env_var": "MY_MCP_TOKEN"},
        )
        assert record["headers"]["authorization"] == "Bearer s3cr3t-value"

    asyncio.run(scenario())


def test_a_missing_token_variable_fails_the_connect_with_its_name(monkeypatch):
    record: dict = {}
    _install_fakes(monkeypatch, record)
    monkeypatch.delenv("MISSING_TOKEN_VAR", raising=False)
    monkeypatch.setattr("server.mcp.login_shell_env", lambda: {})

    mgr = MCPManager()

    async def scenario():
        await mgr.start_server(
            "remote-srv",
            {"url": "https://example.com/mcp", "bearer_token_env_var": "MISSING_TOKEN_VAR"},
        )
        status = mgr._sessions["remote-srv"]
        assert status["status"] == "error"
        assert "MISSING_TOKEN_VAR" in status["error"]
        assert "headers" not in record  # never connected anonymously

    asyncio.run(scenario())


def test_a_token_only_in_the_login_shell_is_not_used(monkeypatch):
    # The bearer token comes from os.environ only. A variable exported in the
    # shell rc (captured for stdio children, credentials scrubbed) never goes
    # into an Authorization header: the connect fails and names the variable.
    record: dict = {}
    _install_fakes(monkeypatch, record)
    monkeypatch.delenv("SHELL_ONLY_TOKEN", raising=False)
    monkeypatch.setattr("server.mcp.login_shell_env", lambda: {"SHELL_ONLY_TOKEN": "from-zshrc"})

    mgr = MCPManager()

    async def scenario():
        await mgr.start_server(
            "remote-srv",
            {"url": "https://example.com/mcp", "bearer_token_env_var": "SHELL_ONLY_TOKEN"},
        )
        status = mgr._sessions["remote-srv"]
        assert status["status"] == "error"
        assert "SHELL_ONLY_TOKEN" in status["error"]
        assert "headers" not in record

    asyncio.run(scenario())


# --- Persistence: the raw token is never written to mcp_servers.json ------


class _FakeRequest:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


def test_bearer_token_value_never_persisted_only_env_var_name(isolated_mcp, monkeypatch):
    mgr = MCPManager()

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(mgr, "start_server", _noop)
    monkeypatch.setattr(mcp_routes, "mcp_manager", mgr)

    os.environ["ADD_SERVER_TOKEN"] = "top-secret-do-not-store"
    try:
        asyncio.run(
            mcp_add_server(
                _FakeRequest(
                    {
                        "name": "remote-srv",
                        "url": "https://example.com/mcp",
                        "bearer_token_env_var": "ADD_SERVER_TOKEN",
                    }
                )
            )
        )
    finally:
        os.environ.pop("ADD_SERVER_TOKEN", None)

    with open(isolated_mcp) as f:
        raw_text = f.read()
    on_disk = json.loads(raw_text)["servers"]

    assert on_disk["remote-srv"]["bearer_token_env_var"] == "ADD_SERVER_TOKEN"
    assert on_disk["remote-srv"]["url"] == "https://example.com/mcp"
    # The schema has no field for the raw token value at all, and the actual
    # secret string never appears anywhere in the persisted file.
    assert "top-secret-do-not-store" not in raw_text
    assert "bearer_token" not in raw_text.replace("bearer_token_env_var", "")
