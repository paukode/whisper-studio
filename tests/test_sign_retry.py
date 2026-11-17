"""The release build's calls to Apple, run in bash against stand-in tools.
A Developer ID signature needs a timestamp from Apple's service and a
release needs the notary service; one unanswered request to either used to
fail a whole release build."""

import subprocess
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "macapp" / "sign_retry.sh"
MACAPP = HELPER.parent

NO_ANSWER = "app.dmg: The timestamp service is not available."
NOT_FOUND = "Chrome: A timestamp was expected but was not found."


def _sign(tmp_path: Path, failures: int, message: str) -> tuple[subprocess.CompletedProcess, int]:
    """Run codesign_retry against a codesign that fails ``failures`` times
    with ``message`` and then succeeds. Returns the run and the call count."""
    tools = tmp_path / "bin"
    tools.mkdir()
    calls = tmp_path / "calls"
    stand_in = tools / "codesign"
    stand_in.write_text(
        "#!/bin/sh\n"
        f'n=$(cat "{calls}" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "{calls}"\n'
        f'if [ "$n" -le {failures} ]; then echo "{message}" >&2; exit 1; fi\n'
        'echo "replacing existing signature" >&2\n'
    )
    stand_in.chmod(0o755)
    run = subprocess.run(
        ["/bin/bash", "-c", f'set -euo pipefail; . "{HELPER}"; codesign_retry --sign id app.dmg'],
        capture_output=True,
        text=True,
        env={"PATH": f"{tools}:/usr/bin:/bin", "SIGN_RETRY_WAIT": "0"},
    )
    return run, int(calls.read_text())


def test_a_quiet_success_signs_once(tmp_path):
    run, calls = _sign(tmp_path, 0, NO_ANSWER)
    assert run.returncode == 0 and calls == 1
    assert run.stderr == ""


def test_an_unanswered_timestamp_request_is_tried_again(tmp_path):
    (tmp_path / "a").mkdir()
    run, calls = _sign(tmp_path / "a", 2, NO_ANSWER)
    assert run.returncode == 0 and calls == 3
    assert "attempt 2 of 5" in run.stderr
    (tmp_path / "b").mkdir()
    run, calls = _sign(tmp_path / "b", 1, NOT_FOUND)
    assert run.returncode == 0 and calls == 2


def test_a_service_that_stays_silent_fails_after_five_attempts(tmp_path):
    run, calls = _sign(tmp_path, 9, NO_ANSWER)
    assert run.returncode == 1 and calls == 5
    assert "5 times" in run.stderr


def test_another_signing_error_is_final_at_once(tmp_path):
    run, calls = _sign(tmp_path, 9, "app.dmg: no identity found")
    assert run.returncode == 1 and calls == 1
    assert "no identity found" in run.stderr


def test_both_build_scripts_sign_through_the_helper():
    """Every signing call of the build goes through codesign_retry; the bare
    codesign calls left only read a signature."""
    for name in ("build_app.sh", "make_dmg.sh"):
        text = (MACAPP / name).read_text()
        assert "sign_retry.sh" in text
        bare = [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("codesign ") and "--verify" not in line
        ]
        assert bare == []


BUSY = "Error: HTTP status code: 500. Internal Server Error\n Please try again at a later time."


def _notarize(
    tmp_path: Path, failures: int, message: str
) -> tuple[subprocess.CompletedProcess, int]:
    """Run notarize.sh on a stand-in DMG against a notary service that fails
    ``failures`` times with ``message`` and then accepts. Returns the run and
    the number of submissions."""
    tools = tmp_path / "bin"
    tools.mkdir()
    calls = tmp_path / "calls"
    codesign = tools / "codesign"
    codesign.write_text('#!/bin/sh\necho "Authority=Developer ID Application: Demo" >&2\n')
    xcrun = tools / "xcrun"
    xcrun.write_text(
        "#!/bin/sh\n"
        '[ "$1" = notarytool ] && [ "$2" = submit ] || exit 0\n'
        f'n=$(cat "{calls}" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "{calls}"\n'
        f'if [ "$n" -le {failures} ]; then printf \'%s\\n\' "{message}" >&2; exit 1; fi\n'
        'echo \'{"id": "demo-1", "status": "Accepted"}\'\n'
    )
    spctl = tools / "spctl"  # Gatekeeper's check of the stapled result
    spctl.write_text("#!/bin/sh\nexit 0\n")
    for tool in (codesign, xcrun, spctl):
        tool.chmod(0o755)
    dmg = tmp_path / "Demo.dmg"
    dmg.write_bytes(b"dmg")
    run = subprocess.run(
        ["/bin/bash", str(MACAPP / "notarize.sh"), str(dmg)],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{tools}:/usr/bin:/bin",
            "NOTARY_PROFILE": "demo",
            "NOTARY_RETRY_WAIT": "0",
        },
    )
    return run, int(calls.read_text())


def test_a_busy_notary_service_is_asked_again(tmp_path):
    run, calls = _notarize(tmp_path, 2, BUSY)
    assert run.returncode == 0, run.stderr
    assert calls == 3
    assert "attempt 2 of 5" in run.stderr
    assert "Accepted" in run.stdout


def test_a_notary_service_that_stays_busy_fails_after_five_submissions(tmp_path):
    run, calls = _notarize(tmp_path, 9, BUSY)
    assert run.returncode == 1 and calls == 5
    assert "notarytool submit failed" in run.stderr


def test_a_refused_notary_login_is_final_at_once(tmp_path):
    run, calls = _notarize(tmp_path, 9, "Error: HTTP status code: 401. Invalid credentials.")
    assert run.returncode == 1 and calls == 1
