"""setup.sh's own helpers, run in bash against stand-in tools. A source install
is the path a dev Mac never exercises (the Mac app bundles its own runtime), so
these pin what it does on a fresh machine."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

SETUP_PATH = Path(__file__).resolve().parents[1] / "setup.sh"
SETUP = SETUP_PATH.read_text()
LINES = [line.strip() for line in SETUP.splitlines()]


def _function(name: str) -> str:
    return re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", SETUP, re.S | re.M).group(0)


def _stand_in(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _bash(script: str, env: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", "-c", script], capture_output=True, text=True, env=env, cwd=cwd
    )


def _run_platform_check(
    tmp_path: Path, system: str, machine: str, macos: str = "26.0"
) -> subprocess.CompletedProcess:
    """require_macos_arm64 from setup.sh, against uname and sw_vers stand-ins
    that report the given OS (uname -s), CPU (uname -m) and macOS version."""
    tools = tmp_path / "bin"
    _stand_in(tools / "uname", f'[ "$1" = -m ] && echo "{machine}" || echo "{system}"')
    _stand_in(tools / "sw_vers", f'echo "{macos}"')
    script = _function("require_macos_arm64") + "require_macos_arm64\necho reached\n"
    return _bash(script, {"PATH": f"{tools}:/usr/bin:/bin"})


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


def test_setup_refuses_macos_older_than_14(tmp_path):
    out = _run_platform_check(tmp_path, "Darwin", "arm64", macos="13.6.1")

    assert out.returncode == 1
    assert "reached" not in out.stdout
    assert out.stderr.strip() == "Whisper Studio requires macOS 14 or later. Detected: 13.6.1."


@pytest.mark.parametrize("macos", ["14.0", "15.4", "26.0"])
def test_setup_continues_on_macos_14_or_later_on_apple_silicon(tmp_path, macos):
    out = _run_platform_check(tmp_path, "Darwin", "arm64", macos=macos)

    assert out.returncode == 0
    assert out.stdout.strip() == "reached"
    assert out.stderr == ""


def test_the_checks_run_before_anything_is_deleted_or_installed():
    """The platform check is the first command, and the prerequisite check runs
    before --new deletes anything and before the first install."""
    first_call = next(
        line for line in LINES if re.fullmatch(r"(require|check|refuse)_[a-z0-9_]+", line)
    )
    assert first_call == "require_macos_arm64"
    checks = LINES.index("check_prerequisites")
    assert checks < LINES.index('if [ "$FRESH" -eq 1 ]; then')
    first_install = next(i for i, line in enumerate(LINES) if line.startswith("run_quiet "))
    assert checks < first_install


@pytest.mark.parametrize("var", ["WHISPER_HOME", "WHISPER_USER_DIR"])
def test_an_exported_data_root_is_refused(var):
    """Either variable would point the source install at another data root,
    such as the Mac app's."""
    script = _function("refuse_foreign_data_root") + "refuse_foreign_data_root\necho reached\n"

    out = _bash(script, {"PATH": "/usr/bin:/bin", var: "/tmp/elsewhere"})

    assert out.returncode == 1
    assert "reached" not in out.stdout
    assert out.stderr.strip() == (
        f"ERROR: {var} is set (/tmp/elsewhere). A source install keeps its data in this "
        f"checkout. Run: unset {var}"
    )


def test_no_data_root_passes():
    script = _function("refuse_foreign_data_root") + "refuse_foreign_data_root\necho reached\n"

    out = _bash(script, {"PATH": "/usr/bin:/bin"})

    assert out.returncode == 0
    assert out.stdout.strip() == "reached"


def _run_venv_python_check(python: str) -> subprocess.CompletedProcess:
    """_require_venv_python from setup.sh against the given interpreter."""
    script = (
        _function("_python_loads_extensions")
        + _function("_require_venv_python")
        + f'_require_venv_python "{python}" "ADVICE"\necho reached\n'
    )
    return _bash(script, {"PATH": "/usr/bin:/bin"})


def test_a_python_that_is_not_arm64_is_refused(tmp_path):
    """An x86_64 Python (one that runs under Rosetta even from a native arm64
    shell) stops setup before a venv is built from it."""
    python = tmp_path / "bin" / "python3"
    _stand_in(python, "exit 1")

    out = _run_venv_python_check(str(python))

    assert out.returncode == 1
    assert "reached" not in out.stdout
    assert out.stderr.strip() == f"ERROR: {python} is not an arm64 (Apple silicon) Python. ADVICE"


def test_a_python_that_is_not_3_13_is_refused(tmp_path):
    python = tmp_path / "bin" / "python3"
    _stand_in(python, 'case "$2" in *platform*) exit 0 ;; *) exit 1 ;; esac')

    out = _run_venv_python_check(str(python))

    assert out.returncode == 1
    assert out.stderr.strip() == f"ERROR: {python} is not Python 3.13. ADVICE"


def test_a_python_that_cannot_load_sqlite_extensions_is_refused(tmp_path):
    """No quiet switch to a slower vector search: the index needs sqlite-vec."""
    python = tmp_path / "bin" / "python3"
    _stand_in(python, '[ "$1" = - ] && exit 1\nexit 0')

    out = _run_venv_python_check(str(python))

    assert out.returncode == 1
    assert out.stderr.strip() == (
        f"ERROR: {python} cannot load SQLite extensions, which the index needs (sqlite-vec). ADVICE"
    )


def test_a_missing_python_is_refused():
    out = _run_venv_python_check("")

    assert out.returncode == 1
    assert "reached" not in out.stdout


@pytest.mark.skipif(sys.version_info[:2] != (3, 13), reason="the check accepts only Python 3.13")
def test_the_checks_accept_this_tests_own_interpreter():
    """The embedded Python snippets are valid: the suite's own arm64 3.13
    interpreter (built to load SQLite extensions) passes all three checks."""
    out = _run_venv_python_check(sys.executable)

    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "reached"


def test_a_new_venv_is_checked_before_it_is_created():
    check = LINES.index(
        '_require_venv_python "$BREW_PYTHON" "Reinstall it with: brew reinstall python@3.13"'
    )
    create = LINES.index('"$BREW_PYTHON" -m venv "$VENV_DIR"')
    assert check < create


def test_a_kept_venv_is_checked_before_it_is_used():
    """A venv/ kept from an earlier run gets the same checks before it is
    activated: one built under Rosetta would otherwise be reused as is."""
    check = LINES.index(
        '_require_venv_python "$VENV_DIR/bin/python" "Delete $VENV_DIR/ and re-run: bash setup.sh"'
    )
    activate = LINES.index('source "$VENV_DIR/bin/activate"')
    assert check < activate


def test_new_rebuilds_the_install_and_never_touches_config_user_json(tmp_path):
    """--new removes what setup.sh installs and resets pricing.json (with a
    backup). config.user.json, the user's settings, stays byte for byte."""
    block = re.search(r'^if \[ "\$FRESH" -eq 1 \]; then\n.*?^fi\n', SETUP, re.S | re.M).group(0)
    (tmp_path / "config.user.json").write_text('{"model_mode": "cloud"}')
    (tmp_path / "pricing.json").write_text('{"x": 1}')
    for d in ("venv", "node_modules", "static/dist", "vendor/llama"):
        (tmp_path / d).mkdir(parents=True)
    script = "FRESH=1\nVENV_DIR=venv\nNODE_MODULES_DIR=node_modules\n" + block

    out = _bash(script, {"PATH": "/usr/bin:/bin"}, cwd=tmp_path)

    assert out.returncode == 0, out.stderr
    assert (tmp_path / "config.user.json").read_text() == '{"model_mode": "cloud"}'
    assert not (tmp_path / "pricing.json").exists()
    assert [p.read_text() for p in tmp_path.glob("pricing.json.bak.*")] == ['{"x": 1}']
    for d in ("venv", "node_modules", "static/dist", "vendor"):
        assert not (tmp_path / d).exists()


@pytest.mark.parametrize("flag", ["--cloud", "--hybrid", "--local", "--fresh"])
def test_removed_flags_are_unknown(tmp_path, flag):
    """A fresh install always starts in Local mode; the mode is changed in
    Settings, not by a setup flag."""
    (tmp_path / ".nvmrc").write_text("22.16.0\n")
    keep = tmp_path / "config.user.json"
    keep.write_text("{}")

    out = subprocess.run(
        ["/bin/bash", str(SETUP_PATH), flag],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp_path)},
    )

    assert out.returncode == 1
    assert f"Unknown option: {flag}" in out.stdout
    assert keep.read_text() == "{}"
