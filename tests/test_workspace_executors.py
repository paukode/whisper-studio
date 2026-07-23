"""Unit tests for the pure helpers in server/workspace/executors.py.

`_is_read_only_command` gates whether ws_run_command bypasses the approval
dialog, so its classification (including the `cd X && cmd` and pipe cases)
is security-relevant and pinned here. `_normalize_quotes` /
`_replace_with_normalization` back the typographic-quote-tolerant edit path.
"""

import json

from server.workspace import executors
from server.workspace.executors import (
    _exec_ws_edit_file,
    _is_read_only_command,
    _normalize_quotes,
    _replace_with_normalization,
)


def test_read_only_simple_commands():
    assert _is_read_only_command("git status") is True
    assert _is_read_only_command("ls -la") is True
    assert _is_read_only_command("cat file.txt") is True


def test_read_only_rejects_mutating_commands():
    assert _is_read_only_command("rm -rf /") is False
    assert _is_read_only_command("npm install") is False
    assert _is_read_only_command("git push") is False


def test_read_only_strips_leading_cd():
    assert _is_read_only_command("cd /tmp && ls") is True
    assert _is_read_only_command("cd /tmp && rm x") is False


def test_read_only_pipe_requires_all_segments():
    assert _is_read_only_command("cat a | grep b") is True
    # First command mutates → not read-only even though grep is.
    assert _is_read_only_command("rm a | grep b") is False
    # A read-only head piped into an interpreter must NOT bypass approval:
    # every pipe segment has to be read-only.
    assert _is_read_only_command("echo payload | bash") is False
    assert _is_read_only_command("cat script.sh | sh") is False


def test_read_only_rejects_interpreters():
    # Interpreters run arbitrary code, so they always require approval.
    assert _is_read_only_command('python3 -c "import os"') is False
    assert _is_read_only_command('python -c "print(1)"') is False
    assert _is_read_only_command('node -e "process.exit()"') is False
    assert _is_read_only_command("awk 'BEGIN{system(\"id\")}'") is False


def test_read_only_rejects_output_redirection():
    # A redirect to a real file is a write; it must not auto-run.
    assert _is_read_only_command("echo pwned > ~/.bashrc") is False
    assert _is_read_only_command("printf x >> file") is False
    # Harmless stderr merges / /dev/null sinks stay read-only.
    assert _is_read_only_command("grep foo bar 2>/dev/null") is True
    assert _is_read_only_command("tail -f log 2>&1") is True


def test_read_only_rejects_find_write_actions():
    assert _is_read_only_command("find . -name '*.py'") is True
    assert _is_read_only_command("find . -delete") is False
    assert _is_read_only_command("find . -exec rm {} ;") is False


def test_normalize_quotes_maps_typographic_to_ascii():
    assert _normalize_quotes("“hello”") == '"hello"'
    assert _normalize_quotes("it’s") == "it's"
    assert _normalize_quotes("«x»") == '"x"'


def test_normalize_quotes_noop_on_ascii():
    assert _normalize_quotes('"plain" text') == '"plain" text'


def test_replace_with_normalization_matches_through_smart_quotes():
    # old_string uses ASCII quotes; original uses curly quotes. The
    # normalized match should locate and replace the curly-quote span.
    original = "foo “bar” baz"
    result = _replace_with_normalization(original, '"bar"', "X", replace_all=False)
    assert result == "foo X baz"


def test_replace_with_normalization_replace_all():
    original = "‘a’ and ‘a’"
    result = _replace_with_normalization(original, "'a'", "Z", replace_all=True)
    assert result == "Z and Z"


def test_replace_with_normalization_no_match_returns_original():
    original = "nothing to see"
    assert _replace_with_normalization(original, "absent", "X", False) == original


def _parse_ws_approval(output: str) -> dict:
    assert output.startswith("[WS_APPROVAL]"), output
    return json.loads(output[len("[WS_APPROVAL]") :])


def _setup_edit(tmp_path, monkeypatch, contents: str):
    """Wire up a connected workspace with one file, bypassing the read gate."""
    f = tmp_path / "note.txt"
    f.write_text(contents)
    monkeypatch.setattr(executors, "get_workspace_path", lambda: str(tmp_path))
    monkeypatch.setattr("server.file_state.check_write_allowed", lambda *a, **k: (True, ""))
    return f


def test_edit_file_flags_quote_normalization(tmp_path, monkeypatch):
    # File has curly quotes; old_string uses ASCII quotes → only the
    # normalized match succeeds, so the payload must carry the hint flag.
    _setup_edit(tmp_path, monkeypatch, "say “hi” now")
    out = _exec_ws_edit_file(
        {"path": "note.txt", "old_string": '"hi"', "new_string": '"bye"'}, [], []
    )
    payload = _parse_ws_approval(out)
    assert payload["matched_via_normalization"] is True
    assert payload["action"] == "write"


def test_edit_file_exact_match_omits_normalization_flag(tmp_path, monkeypatch):
    # Exact match → no fuzzy fallback → flag must be absent so the banner
    # does not show a misleading hint.
    _setup_edit(tmp_path, monkeypatch, "say hi now")
    out = _exec_ws_edit_file({"path": "note.txt", "old_string": "hi", "new_string": "bye"}, [], [])
    payload = _parse_ws_approval(out)
    assert "matched_via_normalization" not in payload
