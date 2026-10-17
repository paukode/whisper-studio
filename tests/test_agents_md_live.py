"""AGENTS.md (the ecosystem-standard instructions filename) honored live.

A repo's AGENTS.md reaches the prompt on every turn as a read-only supplement
from the workspace root, with the project's own WHISPER.md (under
~/.whisper/rules/<slug>/) always winning: it loads alongside under an explicit
precedence note, and is skipped when its text is already inside the project
file (the post-import state).
"""

import os

from server.prompts.rules import project_profile_path
from server.whisper_md import get_whisper_md_context


def _mk(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def test_root_agents_md_alone_serves_as_project_instructions(tmp_path):
    _mk(str(tmp_path / "AGENTS.md"), "Use uv for installs.")
    ctx = get_whisper_md_context(str(tmp_path))
    assert "Use uv for installs." in ctx
    assert "AGENTS.md" in ctx


def test_both_load_and_whisper_md_wins(tmp_path):
    ws = str(tmp_path)
    _mk(project_profile_path(ws), "Native rules.")
    _mk(os.path.join(ws, "AGENTS.md"), "Compat rules.")
    ctx = get_whisper_md_context(ws)
    assert "Native rules." in ctx
    assert "Compat rules." in ctx
    assert ctx.index("Native rules.") < ctx.index("Compat rules.")
    assert "WHISPER.md wins" in ctx


def test_imported_agents_md_is_not_loaded_twice(tmp_path):
    ws = str(tmp_path)
    _mk(os.path.join(ws, "AGENTS.md"), "Shared imported text.")
    _mk(project_profile_path(ws), "Header\n\nShared imported text.")
    ctx = get_whisper_md_context(ws)
    assert ctx.count("Shared imported text.") == 1


def test_a_nested_agents_md_is_not_read(tmp_path):
    ws = str(tmp_path)
    _mk(os.path.join(ws, "docs", "AGENTS.md"), "docs compat rules")
    assert get_whisper_md_context(ws) == ""
