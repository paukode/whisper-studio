"""A login shell that is a wrapper still shows what runs inside it.

A terminal integration (``zsh (kiro-cli-term)`` in a field session) starts
the user's real shell on a second, inner PTY. The outer PTY then always shows
the wrapper, idle, in its foreground: a timed-out ``sleep 120`` looked over,
so terminal_send refused the Ctrl-C that would stop it. The inner terminal's
foreground group says what runs there.
"""

import subprocess

import pytest

from server.executors import terminal_run


def _ps(rows: list[tuple[int, int, int, int, str, str]]):
    text = "\n".join(f"{p} {pp} {g} {tg} {tty} {comm}" for p, pp, g, tg, tty, comm in rows)
    return lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=text + "\n", stderr="")


# The outer login shell (100) on ttys001; the wrapper's inner zsh (110) on ttys002.
_OUTER = (100, 1, 100, 100, "ttys001", "zsh (kiro-cli-term)")
_HELPER = (105, 100, 105, 0, "??", "kiro-cli")


@pytest.mark.parametrize(
    "inner, expected",
    [
        ([(110, 100, 110, 110, "ttys002", "/bin/zsh")], None),  # inner prompt
        (
            [
                (110, 100, 110, 120, "ttys002", "/bin/zsh"),
                (120, 110, 120, 120, "ttys002", "sleep"),
            ],
            "sleep",
        ),
        ([], None),  # no inner terminal: a plain shell
    ],
)
def test_the_inner_terminal_decides(monkeypatch, inner, expected):
    monkeypatch.setattr(subprocess, "run", _ps([_OUTER, _HELPER, *inner]))
    assert terminal_run._wrapped_foreground(100) == expected


def test_ps_failing_reads_as_idle(monkeypatch):
    def boom(*a, **k):
        raise OSError("no ps")

    monkeypatch.setattr(subprocess, "run", boom)
    assert terminal_run._wrapped_foreground(100) is None
