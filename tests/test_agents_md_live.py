"""AGENTS.md (the ecosystem-standard instructions filename) honored live.

Before this, a repo's AGENTS.md only reached prompts through the one-time
importer, which goes stale as the repo evolves. Now the loader reads it on
every turn as a read-only supplement with WHISPER.md always winning: root
AGENTS.md loads alongside root WHISPER.md under an explicit precedence note
(and is skipped when already imported verbatim), and a scoped WHISPER.md
shadows a sibling AGENTS.md outright.
"""

import os

from server.whisper_md import find_scoped_files, get_whisper_md_context


def _mk(tmp_path, rel, content):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_root_agents_md_alone_serves_as_project_instructions(tmp_path):
    _mk(tmp_path, "AGENTS.md", "Use uv for installs.")
    ctx = get_whisper_md_context(str(tmp_path))
    assert "Use uv for installs." in ctx
    assert "AGENTS.md" in ctx


def test_both_root_files_load_and_whisper_md_wins(tmp_path):
    _mk(tmp_path, "WHISPER.md", "Native rules.")
    _mk(tmp_path, "AGENTS.md", "Compat rules.")
    ctx = get_whisper_md_context(str(tmp_path))
    assert "Native rules." in ctx
    assert "Compat rules." in ctx
    # WHISPER.md is primary (first), and the AGENTS.md block states the
    # precedence explicitly rather than relying on ordering.
    assert ctx.index("Native rules.") < ctx.index("Compat rules.")
    assert "WHISPER.md wins" in ctx


def test_imported_agents_md_is_not_loaded_twice(tmp_path):
    # Post-import state: the importer copied AGENTS.md verbatim into
    # WHISPER.md, so loading both would duplicate the same text every turn.
    _mk(tmp_path, "AGENTS.md", "Shared imported text.")
    _mk(tmp_path, "WHISPER.md", "Header\n\nShared imported text.")
    ctx = get_whisper_md_context(str(tmp_path))
    assert ctx.count("Shared imported text.") == 1


def test_scoped_whisper_md_shadows_sibling_agents_md(tmp_path):
    _mk(tmp_path, "server/WHISPER.md", "native scoped")
    _mk(tmp_path, "server/AGENTS.md", "compat scoped")
    _mk(tmp_path, "docs/AGENTS.md", "docs compat")
    scoped = find_scoped_files(str(tmp_path))
    assert os.path.join("server", "WHISPER.md") in scoped
    assert os.path.join("server", "AGENTS.md") not in scoped
    assert os.path.join("docs", "AGENTS.md") in scoped


def test_scoped_agents_md_inlines_when_the_turn_concerns_its_directory(tmp_path):
    _mk(tmp_path, "docs/AGENTS.md", "docs compat rules")
    ctx = get_whisper_md_context(str(tmp_path), question="update the docs build")
    assert "docs compat rules" in ctx


def test_scoped_agents_md_defers_when_irrelevant(tmp_path):
    _mk(tmp_path, "docs/AGENTS.md", "docs compat rules")
    ctx = get_whisper_md_context(str(tmp_path), question="fix the auth bug")
    assert "docs compat rules" not in ctx
    assert os.path.join("docs", "AGENTS.md") in ctx  # announced by path
