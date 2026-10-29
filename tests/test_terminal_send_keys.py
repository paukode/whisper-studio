"""terminal_send passes Ctrl-C, Ctrl-D and Ctrl-Q sent on their own.

The tool always said it passes control bytes through, but the shell
validator's control-character check refused a lone \\x03, so a field session
could not stop the program it had started in its terminal.
"""

from __future__ import annotations

import asyncio

import pytest

from server.executors.terminal_run import do_terminal_send


@pytest.fixture
def typed(monkeypatch):
    seen: list[str] = []

    async def send_and_wait(session, text, timeout):
        seen.append(text)
        return {"wait_reason": "inferred_idle", "output": ""}

    monkeypatch.setattr("server.terminal.get_or_open_interactive", lambda *a, **k: object())
    monkeypatch.setattr("server.terminal.send_and_wait", send_and_wait)
    return seen


def _send(text: str) -> tuple[bool, str]:
    return asyncio.run(do_terminal_send({"__session_id__": "c1", "input": text}))


@pytest.mark.parametrize("keys", ["\x03", "\x04", "\x11", "\x03\x03"])
def test_a_key_sent_on_its_own_reaches_the_terminal(typed, keys):
    ok, _ = _send(keys)
    assert ok and typed == [keys]


@pytest.mark.parametrize(
    "text",
    [
        "\x03rm -rf build",
        "\x03\nr\\m -rf build",
        "\x1b",
        "\x10\r",  # Ctrl-P then Enter runs the last command in the history again
    ],
)
def test_other_control_bytes_are_refused(typed, text):
    ok, message = _send(text)
    assert not ok and "control character" in message
    assert typed == []
