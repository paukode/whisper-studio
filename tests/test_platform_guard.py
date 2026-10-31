"""The platform guard (server/infrastructure/platform_guard.py): Whisper Studio
runs only on macOS on Apple silicon (arm64). Everywhere else it fails closed
with one clear message, and both entrypoints (the server and the launchd index
worker) make the check before they do anything else."""

import platform
import runpy
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from server.infrastructure import platform_guard

REPO = Path(__file__).resolve().parents[1]


def _message(system: str, machine: str) -> str:
    return f"Whisper Studio requires macOS on Apple silicon (arm64). Detected: {system}/{machine}."


def _pretend(monkeypatch, sys_platform: str, system: str, machine: str) -> None:
    monkeypatch.setattr(sys, "platform", sys_platform)
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)


@pytest.mark.parametrize("machine", ["x86_64", "aarch64", "arm64"])
def test_refuses_linux(monkeypatch, machine):
    _pretend(monkeypatch, "linux", "Linux", machine)

    with pytest.raises(SystemExit) as exc:
        platform_guard.require_macos_arm64()

    assert exc.value.code == _message("Linux", machine)


@pytest.mark.parametrize(
    ("sys_platform", "system", "machine"),
    [
        ("win32", "Windows", "AMD64"),
        ("freebsd14", "FreeBSD", "arm64"),
        ("cygwin", "CYGWIN", "x86_64"),
    ],
)
def test_refuses_every_other_os(monkeypatch, sys_platform, system, machine):
    _pretend(monkeypatch, sys_platform, system, machine)

    with pytest.raises(SystemExit) as exc:
        platform_guard.require_macos_arm64()

    assert exc.value.code == _message(system, machine)


def test_refuses_an_intel_mac_or_rosetta(monkeypatch):
    # An Intel Mac, and an x86_64 Python under Rosetta on Apple silicon, both
    # report x86_64.
    _pretend(monkeypatch, "darwin", "Darwin", "x86_64")

    with pytest.raises(SystemExit) as exc:
        platform_guard.require_macos_arm64()

    assert exc.value.code == _message("Darwin", "x86_64")


def test_passes_on_macos_on_apple_silicon(monkeypatch):
    _pretend(monkeypatch, "darwin", "Darwin", "arm64")

    assert platform_guard.require_macos_arm64() is None


def test_the_server_refuses_before_it_imports_any_third_party_module():
    """Importing server.main on an unsupported machine stops at the guard:
    fastapi (the first third-party import) never loads."""
    probe = (
        "import asyncio, contextlib, logging, os, platform, sys, threading\n"
        "sys.platform = 'linux'\n"
        "platform.system = lambda: 'Linux'\n"
        "platform.machine = lambda: 'x86_64'\n"
        "try:\n"
        "    import server.main\n"
        "except SystemExit as exc:\n"
        "    print(exc.code)\n"
        "    print('fastapi' in sys.modules)\n"
        "else:\n"
        "    print('server.main imported')\n"
    )

    out = subprocess.run(
        [sys.executable, "-c", probe], cwd=REPO, capture_output=True, text=True, timeout=120
    )

    assert out.stdout.splitlines() == [_message("Linux", "x86_64"), "False"], out.stderr


def test_the_index_worker_refuses_before_it_runs_any_action(monkeypatch, capsys):
    """launchd runs `python -m server.index.agent run` on its own: on an
    unsupported machine it exits with the guard's message and runs nothing."""
    import server.index.agent  # noqa: F401 - loaded first, as `python -m` does

    _pretend(monkeypatch, "linux", "Linux", "x86_64")
    monkeypatch.setattr(sys, "argv", ["server.index.agent", "status"])

    with warnings.catch_warnings():
        # runpy warns that the module is already in sys.modules; expected here.
        warnings.simplefilter("ignore", RuntimeWarning)
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("server.index.agent", run_name="__main__")

    assert exc.value.code == _message("Linux", "x86_64")
    assert capsys.readouterr().out == ""
