"""`IFS= read` and a backslash inside quotes are not obfuscation.

A field session's `while IFS= read -r file` loop was refused as a dangerous
environment variable, and a `node -e "...split(/\\r?\\n/)..."` one-liner as a
backslash-escaped character. Neither can hide a command; the tricks the two
checks exist for are still caught.
"""

from __future__ import annotations

import pytest

from server.security.command_validator import validate_command


@pytest.mark.parametrize(
    "command",
    [
        'git ls-files --others | while IFS= read -r file; do wc -l "$file"; done',
        "IFS=, read -r a b < pairs.csv",
        "IFS=$'\\t' read -r a b < rows.tsv",
        'node -e "const lines = s.split(/\\r?\\n/); console.log(lines.length)"',
        'grep -E "\\d{4}-\\d{2}" app.log',
    ],
)
def test_passes(command):
    assert validate_command(command) is None


@pytest.mark.parametrize(
    ("command", "says"),
    [
        # IFS set for the shell changes how every later word splits.
        ("IFS=/ ; cmd=usr/bin/id; $cmd", "IFS="),
        ("export IFS=:", "IFS="),
        # The shell drops these backslashes and runs rm.
        ("r\\m -rf build", "backslash"),
        ('FOO=1 "r"\\m -rf build', "backslash"),
        ("FOO=1 'r'\\m -rf build", "backslash"),
        ("r''\\m -rf build", "backslash"),
    ],
)
def test_still_blocked(command, says):
    warning = validate_command(command)
    assert warning and says in warning
