"""macapp/sign_retry.sh, run in bash against a stand-in codesign. A Developer
ID signature needs a timestamp from Apple's service, and one unanswered
request used to fail a whole release build."""

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
