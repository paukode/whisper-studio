"""The completion marker must go on its own PTY input line.

Regression: run_in_session appended the marker with ``;`` on the SAME line as
the command:  ``{command}; printf '...__CLAUDE_DONE_...' $?``.  When the
command's last line ended in a ``#`` comment (models emit these), the trailing
``; printf`` was commented out, the marker never printed, and the poll loop
hung to the full timeout — reporting timed_out for a command that succeeded.
The fix puts the printf on a separate input line.
"""

from server.terminal import _build_run_payload


def test_marker_is_on_a_separate_line_from_command():
    marker = "deadbeefcafebabe"
    command = "echo hello"
    payload = _build_run_payload(command, marker)

    # The command and the marker printf must be on different lines.
    lines = payload.split("\n")
    cmd_line_idx = next(i for i, ln in enumerate(lines) if command in ln)
    printf_line_idx = next(i for i, ln in enumerate(lines) if "printf" in ln)
    assert printf_line_idx > cmd_line_idx

    # Nothing appears after the command on its own line (no `; printf`).
    assert lines[cmd_line_idx].strip() == command
    assert ";" not in lines[cmd_line_idx]


def test_trailing_comment_cannot_consume_the_marker():
    marker = "0123456789abcdef"
    # A command whose last line is a comment — the classic failure case.
    command = "ls -la  # list the directory"
    payload = _build_run_payload(command, marker)

    # The printf must live on a line that is NOT the comment line, so the shell
    # still executes it.
    assert f"\nprintf '\\n__CLAUDE_DONE_{marker}:%d\\n' $?\n" in payload
    # The comment and the printf are not on the same line.
    for line in payload.split("\n"):
        if "#" in line:
            assert "printf" not in line


def test_exit_code_still_captured_via_dollar_question():
    # A newline before printf must not swallow $? — the marker still carries
    # the exit code of the preceding command.
    payload = _build_run_payload("true", "aa11bb22")
    assert "$?" in payload
    assert payload.rstrip("\n").endswith("$?")
