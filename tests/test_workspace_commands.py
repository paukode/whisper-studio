"""Unit tests for server/workspace/commands.py — the shell-execution helpers
shared by the workspace executors and HTTP routes after the package split.
"""

import base64

import server.workspace.commands as C
from server.workspace.commands import (
    _apply_stdin_redirect,
    _detect_image_output,
    _interpret_exit_code,
    _is_silent_command,
    _needs_stdin_redirect,
    _truncate_shell_output,
)


def test_needs_stdin_redirect_default_true():
    assert _needs_stdin_redirect("echo hi") is True


def test_needs_stdin_redirect_false_when_already_redirected():
    assert _needs_stdin_redirect("cat < file") is False
    assert _needs_stdin_redirect("cat <<EOF") is False


def test_apply_stdin_redirect_appends_when_no_pipe():
    assert _apply_stdin_redirect("echo hi") == "echo hi < /dev/null"


def test_apply_stdin_redirect_before_first_pipe():
    assert _apply_stdin_redirect("cat a | grep b") == "cat a < /dev/null | grep b"


def test_apply_stdin_redirect_ignores_quoted_pipe():
    # A pipe inside quotes is not a real pipe — redirect goes to the end.
    out = _apply_stdin_redirect("echo 'a | b'")
    assert out == "echo 'a | b' < /dev/null"


def test_interpret_exit_code_known_pair():
    assert _interpret_exit_code("grep foo bar", 1) == "no matches found"
    assert _interpret_exit_code("git push", 128) == "fatal error"


def test_interpret_exit_code_zero_is_none():
    assert _interpret_exit_code("grep foo bar", 0) is None


def test_interpret_exit_code_unknown_is_none():
    assert _interpret_exit_code("somecmd", 3) is None


def test_is_silent_command():
    assert _is_silent_command("mv a b") is True
    assert _is_silent_command("/bin/rm -rf x") is True
    assert _is_silent_command("ls -la") is False


def test_detect_image_output_finds_png():
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 120
    b64 = base64.b64encode(raw).decode()
    out = _detect_image_output(f"prefix text {b64} suffix")
    assert out is not None
    assert out["mime_type"] == "image/png"
    assert out["data"] == b64


def test_detect_image_output_none_for_plain_text():
    assert _detect_image_output("just some normal command output") is None


def test_truncate_shell_output_passthrough_when_small():
    assert _truncate_shell_output("short") == "short"


def test_truncate_shell_output_truncates_large(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "_SHELL_OUTPUT_DIR", str(tmp_path))
    big = "x" * 60_000
    out = _truncate_shell_output(big)
    assert len(out) < len(big)
    assert "truncated" in out
    assert "Full output saved to:" in out
    # The full payload was persisted to the (monkeypatched) cache dir.
    written = list(tmp_path.glob("output_*.txt"))
    assert len(written) == 1
    assert written[0].read_text() == big
