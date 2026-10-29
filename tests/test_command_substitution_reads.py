"""A read-only command may run inside $(...); its output may not run as a command.

In a field session every commit and merge command checked the branch first,
``test "$(git branch --show-current)" = fix/x && git commit ...``, and each
one was refused as command substitution, also in Bypass. The block exists
because a substitution can build a command the checks never see:
``$(echo cm0= | base64 -d)`` is ``rm``.
"""

from __future__ import annotations

import pytest

from server.security.command_validator import validate_command


@pytest.mark.parametrize(
    "command",
    [
        'test "$(git branch --show-current)" = fix/x && git add -- README.md && git commit -m x',
        'test "$(git rev-parse main)" = "$(git rev-parse fix/x)" && git branch -d fix/x',
        'test -z "$(git status --porcelain)" && git switch main',
        'test "$(git symbolic-ref --short HEAD)" = fix/x && git commit -m x',
        "for attempt in $(seq 1 40); do sleep 60; done",
        # A pipe inside $(...) belongs to the substitution, not the outer command.
        'test "$(git log --oneline | head -1)" = x && git push',
        "printf '%s\\n' \"$(date -u +%FT%TZ)\"",
        'state=$(git rev-parse HEAD); echo "$state"',
        "git commit -m \"$(cat <<'EOF'\nSubject\n\nBody with | pipes and $(text)\nEOF\n)\"",
    ],
)
def test_a_read_only_substitution_passes(command):
    assert validate_command(command) is None


@pytest.mark.parametrize(
    ("command", "says"),
    [
        ("$(echo cm0= | base64 -d) -rf build", "would run as a command"),
        ('FOO=1 "$(echo rm)" -rf build', "would run as a command"),
        ("cd app && $(git config alias.x)", "would run as a command"),
        ('eval "$(cat setup.sh)"', "would run as a command"),
        ('bash -lc "$(cat run.txt)"', "would run as a command"),
        ("ls | xargs $(cat cmd.txt)", "would run as a command"),
        ("echo $(touch made)", "not read-only"),
        # curl can send data and write files, so it is not a read.
        ("state=$(curl -fsS http://127.0.0.1:8781/api/meta)", "not read-only"),
        ("echo $(cat ~/.ssh/id_rsa)", "sensitive path"),
        ('echo "$(git branch --show-current"', "command substitution detected"),
        ("echo `whoami`", "command substitution detected"),
        ("diff <(ls a) <(ls b)", "command substitution detected"),
    ],
)
def test_a_substitution_that_writes_hides_or_runs_its_output_is_blocked(command, says):
    warning = validate_command(command)
    assert warning and says in warning


def test_the_command_inside_follows_the_callers_secret_reads():
    # Bypass may read the credential stores (server/security/secret_reads.py).
    assert validate_command("echo $(cat ~/.ssh/id_rsa)", secret_reads=True) is None
