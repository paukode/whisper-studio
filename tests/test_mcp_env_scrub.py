"""Stdio MCP children must not inherit the host app's credentials.

server/security/sensitive_env.py is the canonical list; explicit per-server
`env` entries in mcp_servers.json are the deliberate opt-in that can still
forward one.
"""

import asyncio

from server import sandbox
from server.mcp import MCPManager
from server.security.sensitive_env import (
    CREDENTIAL_ENV_VARS,
    GITHUB_ENV_VARS,
    scrub_credential_env,
)


def test_scrub_removes_credentials_and_keeps_the_rest():
    environ = {name: "x" for name in CREDENTIAL_ENV_VARS}
    environ["PATH"] = "/usr/bin"
    environ["HOME"] = "/Users/nobody"
    assert scrub_credential_env(environ) == {"PATH": "/usr/bin", "HOME": "/Users/nobody"}


def test_sandbox_denylist_is_the_github_subset():
    # The sandbox deliberately scrubs ONLY the GitHub tokens (sandboxed code
    # authenticates to AWS/Bedrock via inherited env creds); pin that the two
    # consumers keep drawing from the same canonical module.
    assert sandbox._SANDBOX_ENV_DENYLIST == GITHUB_ENV_VARS
    assert GITHUB_ENV_VARS < CREDENTIAL_ENV_VARS


def _start_and_capture(monkeypatch, tmp_path, config):
    """Run start_server with _serve stubbed out; return the StdioServerParameters."""
    exe = tmp_path / "mcp-server"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    config = {"command": str(exe), **config}

    captured = {}

    async def fake_serve(self, name, params, config, ready, stop_event):
        captured["params"] = params
        ready.set_result(None)

    monkeypatch.setattr(MCPManager, "_serve", fake_serve)
    mgr = MCPManager()
    asyncio.run(mgr.start_server("Probe", config))
    return captured["params"]


def test_stdio_child_env_scrubs_inherited_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "host-only")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-only")
    params = _start_and_capture(monkeypatch, tmp_path, {})
    assert "AWS_SECRET_ACCESS_KEY" not in params.env
    assert "ANTHROPIC_API_KEY" not in params.env
    assert "PATH" in params.env  # the scrub is credentials-only


def test_configured_per_server_env_still_forwards(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEY", "host-only")
    params = _start_and_capture(
        monkeypatch,
        tmp_path,
        {"env": {"MY_SERVER_TOKEN": "configured", "TAVILY_API_KEY": "forwarded"}},
    )
    assert params.env["MY_SERVER_TOKEN"] == "configured"
    # A denylisted NAME configured explicitly for this server is the
    # deliberate opt-in — it wins over the scrub.
    assert params.env["TAVILY_API_KEY"] == "forwarded"
