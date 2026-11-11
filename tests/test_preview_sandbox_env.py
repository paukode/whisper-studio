"""A preview dev server gets the same env as every other sandboxed child.

DevServerProcess.spawn runs the dev server under the sandbox profile, so its
env comes from sandbox._merged_env: no GitHub token, and the caller's env
applied last. Each test spawns a real child that prints its environment.
"""

import asyncio

from server.preview.process import DevServerProcess
from server.security.sensitive_env import GITHUB_ENV_VARS


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


def test_dev_server_never_sees_a_github_token(monkeypatch, tmp_path):
    for name in GITHUB_ENV_VARS:
        monkeypatch.setenv(name, "should-never-appear")
    monkeypatch.setenv("WHISPER_PREVIEW_ENV_PROBE", "kept")

    env = _child_env(tmp_path)

    assert not GITHUB_ENV_VARS & env.keys()
    assert "should-never-appear" not in env.values()
    # The rest of the user's env still reaches the dev server.
    assert env["WHISPER_PREVIEW_ENV_PROBE"] == "kept"


def test_caller_env_is_applied_last(monkeypatch, tmp_path):
    monkeypatch.setenv("PORT", "1")

    env = _child_env(tmp_path, env={"PORT": "5173"})

    assert env["PORT"] == "5173"
