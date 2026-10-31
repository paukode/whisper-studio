"""setup.sh's own helpers, run in bash against stand-in tools. A source install
is the path a dev Mac never exercises (the Mac app bundles its own runtime), so
these pin what it does on a fresh machine."""

import platform
import re
import subprocess
import sys
from pathlib import Path

import pytest

SETUP = (Path(__file__).resolve().parents[1] / "setup.sh").read_text()


def _function(name: str) -> str:
    return re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", SETUP, re.S | re.M).group(0)


def _stand_in(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


@pytest.mark.parametrize("version", ["3.13", "3.12"])
def test_an_installed_homebrew_python_builds_the_venv(tmp_path, version):
    """The default python3 cannot load SQLite extensions (python.org's build
    cannot) and Homebrew already has a Python that can: the venv uses it, and
    nothing is installed."""
    tools = tmp_path / "bin"
    prefix = tmp_path / "homebrew"
    calls = tmp_path / "brew-calls"
    _stand_in(tools / "python3", "exit 1")
    _stand_in(
        tools / "brew",
        f'echo "$*" >> "{calls}"\n[ "$1" = --prefix ] && echo "{prefix}"\nexit 0',
    )
    brew_python = prefix / "opt" / f"python@{version}" / "bin" / f"python{version}"
    _stand_in(brew_python, "exit 0")
    script = (
        _function("_python_loads_extensions")
        + _function("_select_venv_python")
        + 'VENV_PYTHON=""\n_select_venv_python >/dev/null\necho "$VENV_PYTHON"\n'
    )

    out = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{tools}:/usr/bin:/bin"},
        check=True,
    )

    assert out.stdout.strip() == str(brew_python)
    assert "install" not in calls.read_text()


def _run_platform_check(tmp_path: Path, system: str, machine: str) -> subprocess.CompletedProcess:
    """require_macos_arm64 from setup.sh, against a uname stand-in that reports
    the given OS (uname -s) and CPU (uname -m)."""
    tools = tmp_path / "bin"
    _stand_in(tools / "uname", f'[ "$1" = -m ] && echo "{machine}" || echo "{system}"')
    script = _function("require_macos_arm64") + "require_macos_arm64\necho reached\n"
    return subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{tools}:/usr/bin:/bin"},
    )


@pytest.mark.parametrize(
    ("system", "machine"),
    [("Linux", "x86_64"), ("Linux", "aarch64"), ("Darwin", "x86_64"), ("FreeBSD", "arm64")],
)
def test_setup_refuses_anything_but_macos_on_apple_silicon(tmp_path, system, machine):
    """Another OS, an Intel Mac or a Rosetta shell (both report x86_64) stops
    with the same message the server's platform guard gives, and nothing after
    the check runs."""
    out = _run_platform_check(tmp_path, system, machine)

    assert out.returncode == 1
    assert "reached" not in out.stdout
    assert out.stderr.strip() == (
        f"Whisper Studio requires macOS on Apple silicon (arm64). Detected: {system}/{machine}."
    )


def test_setup_continues_on_macos_on_apple_silicon(tmp_path):
    out = _run_platform_check(tmp_path, "Darwin", "arm64")

    assert out.returncode == 0
    assert out.stdout.strip() == "reached"
    assert out.stderr == ""


def test_setup_checks_the_platform_before_it_checks_or_installs_anything():
    """The platform check is the first command setup.sh runs, ahead of the
    Python check (which can offer a Homebrew install)."""
    lines = SETUP.splitlines()
    assert lines.index("require_macos_arm64") < lines.index("require_python_312")
    first_call = next(line for line in lines if re.fullmatch(r"(require|check)_[a-z0-9_]+", line))
    assert first_call == "require_macos_arm64"


def _run_arm64_python_check(python: str) -> subprocess.CompletedProcess:
    """_require_arm64_python from setup.sh against the given interpreter."""
    script = (
        _function("_require_arm64_python") + f'_require_arm64_python "{python}"\necho reached\n'
    )
    return subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )


def test_a_venv_python_that_is_not_arm64_is_refused(tmp_path):
    """An x86_64 Python (one that runs under Rosetta even from a native arm64
    shell) stops setup before a venv is built from it."""
    python = tmp_path / "bin" / "python3"
    _stand_in(python, "exit 1")

    out = _run_arm64_python_check(str(python))

    assert out.returncode == 1
    assert "reached" not in out.stdout
    assert out.stderr.strip() == (
        f"ERROR: {python} is not an arm64 (Apple silicon) Python. "
        "Install an arm64 Python (Homebrew under /opt/homebrew) and re-run."
    )


def test_a_missing_venv_python_is_refused():
    out = _run_arm64_python_check("")

    assert out.returncode == 1
    assert "reached" not in out.stdout


def test_an_arm64_venv_python_is_accepted(tmp_path):
    python = tmp_path / "bin" / "python3"
    _stand_in(python, "exit 0")

    out = _run_arm64_python_check(str(python))

    assert out.returncode == 0
    assert out.stdout.strip() == "reached"
    assert out.stderr == ""


def test_the_arm64_check_reads_the_interpreter_architecture():
    """Run against this test's own interpreter, the check agrees with
    platform.machine(), so the embedded Python snippet is valid."""
    out = _run_arm64_python_check(sys.executable)

    assert out.returncode == (0 if platform.machine() == "arm64" else 1)


def test_a_new_venv_is_checked_for_arm64_before_it_is_created():
    lines = [line.strip() for line in SETUP.splitlines()]
    select = lines.index("_select_venv_python")
    check = lines.index('_require_arm64_python "$VENV_PYTHON"')
    create = lines.index('"$VENV_PYTHON" -m venv "$VENV_DIR"')
    assert select < check < create
