"""terminal_run must apply the same command validation as the workspace shell,
in BOTH sandbox and visible modes — rejecting before any shell is spawned."""

import asyncio

from server.executors.terminal_run import do_terminal_run


def test_blocks_dangerous_command_sandbox_mode():
    ok, msg = asyncio.run(do_terminal_run({"command": "rm -rf /", "mode": "sandbox"}))
    assert ok is False
    assert msg


def test_blocks_sensitive_path_read_visible_mode():
    # Rejected by validation before the "no visible session" check is reached.
    ok, msg = asyncio.run(do_terminal_run({"command": "cat ~/.ssh/id_rsa", "mode": "visible"}))
    assert ok is False
    assert msg
