"""A preview dev server gets the same env as every other sandboxed child.

DevServerProcess.spawn runs the dev server under the sandbox profile, which
denies direct 80/443 under a non-permissive network tier. So its env comes
from sandbox._merged_env: no GitHub token, the egress proxy vars under a
non-permissive tier, and the caller's env applied last. Each test spawns a
real child that prints its environment.
"""

import asyncio

import pytest

from server.preview.process import DevServerProcess
from server.security import egress_policy
from server.security.sensitive_env import GITHUB_ENV_VARS

_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def _child_env(cwd, env=None) -> dict[str, str]:
    async def _run():
        dev = await DevServerProcess.spawn(["/usr/bin/env"], cwd=str(cwd), env=env)
        try:
            await asyncio.wait_for(dev.proc.wait(), timeout=10)
            await asyncio.gather(*dev._reader_tasks)
            return dev.stdout_buf.decode("utf-8", "replace")
        finally:
            await dev.stop()

    out = asyncio.run(_run())
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def _set_tier(monkeypatch, tier):
    monkeypatch.setattr(
        egress_policy,
        "get_active_policy",
        lambda: {"tier": tier, "custom_allowed_domains": [], "methods_only_safe": False},
    )
    monkeypatch.setattr(egress_policy, "ensure_proxy_running", lambda: ("127.0.0.1", 54321))


@pytest.mark.parametrize("tier", ["permissive", "curated"])
def test_dev_server_never_sees_a_github_token(monkeypatch, tmp_path, tier):
    _set_tier(monkeypatch, tier)
    for name in GITHUB_ENV_VARS:
        monkeypatch.setenv(name, "should-never-appear")
    monkeypatch.setenv("WHISPER_PREVIEW_ENV_PROBE", "kept")

    env = _child_env(tmp_path)

    assert not GITHUB_ENV_VARS & env.keys()
    assert "should-never-appear" not in env.values()
    # The rest of the user's env still reaches the dev server.
    assert env["WHISPER_PREVIEW_ENV_PROBE"] == "kept"


def test_dev_server_gets_the_egress_proxy_under_a_curated_tier(monkeypatch, tmp_path):
    _set_tier(monkeypatch, "curated")

    env = _child_env(tmp_path)

    for name in _PROXY_VARS:
        assert env[name] == "http://127.0.0.1:54321", name


def test_caller_env_is_applied_last(monkeypatch, tmp_path):
    _set_tier(monkeypatch, "curated")

    env = _child_env(tmp_path, env={"HTTPS_PROXY": "http://custom:9", "PORT": "5173"})

    assert env["HTTPS_PROXY"] == "http://custom:9"
    assert env["PORT"] == "5173"
    assert env["HTTP_PROXY"] == "http://127.0.0.1:54321"
