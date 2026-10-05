"""setup.sh's own helpers, run in bash against stand-in tools. A source install
is the path a dev Mac never exercises (the Mac app bundles its own runtime), so
these pin what it does on a fresh machine."""

import re
import subprocess
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
