"""Per-project instructions load from ~/.whisper/rules/<project-slug>/.

The workspace carries no WHISPER.md for this app any more: the project's
WHISPER.md (overview) and rules.md (rules) sit in the user's own folder,
named with the memory store's slug, so they are never committed and the
assistant can edit them without an approval. A repo's AGENTS.md is still
honored live, read-only, as a supplement.
"""

import os

from server.memory.memdir import get_workspace_slug
from server.prompts.rules import MAX_FILE_CHARS, project_profile_path, project_rules_dir
from server.whisper_md import get_whisper_md_context


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def test_no_workspace_and_no_files_yield_nothing(tmp_path):
    assert get_whisper_md_context(None) == ""
    assert get_whisper_md_context(str(tmp_path)) == ""


def test_project_folder_uses_the_memory_slug(tmp_path):
    ws = str(tmp_path / "app")
    os.makedirs(ws)
    slug = get_workspace_slug(ws)
    assert os.path.basename(project_rules_dir(ws)) == slug
    assert project_profile_path(ws) == os.path.join(project_rules_dir(ws), "WHISPER.md")
    # Two projects that share a basename never share a folder.
    other = str(tmp_path / "elsewhere" / "app")
    os.makedirs(other)
    assert project_rules_dir(other) != project_rules_dir(ws)


def test_a_whisper_md_inside_the_workspace_is_not_read(tmp_path):
    ws = str(tmp_path)
    _write(os.path.join(ws, "WHISPER.md"), "REPO COPY")
    _write(os.path.join(ws, "server", "WHISPER.md"), "SCOPED COPY")
    assert get_whisper_md_context(ws) == ""


def test_project_profile_and_rules_load_with_labels(tmp_path):
    ws = str(tmp_path)
    slug = get_workspace_slug(ws)
    _write(project_profile_path(ws), "This project is a planner.")
    _write(os.path.join(project_rules_dir(ws), "rules.md"), "- Use uv.")
    _write(os.path.join(project_rules_dir(ws), "testing.md"), "- Run pytest first.")

    out = get_whisper_md_context(ws)
    assert f"[rules/{slug}/WHISPER.md, project instructions]" in out
    assert "This project is a planner." in out
    assert f"[rules/{slug}/rules.md, project rules]" in out
    # rules.md leads, other rule files follow in name order.
    assert out.index("Use uv.") < out.index("Run pytest first.")
    assert out.startswith("\n\n")


def test_oversized_files_are_capped(tmp_path):
    ws = str(tmp_path)
    _write(project_profile_path(ws), "x" * (MAX_FILE_CHARS * 3))
    _write(os.path.join(project_rules_dir(ws), "rules.md"), "y" * (MAX_FILE_CHARS * 3))
    out = get_whisper_md_context(ws)
    assert len(out) < 2 * MAX_FILE_CHARS + 1000
    assert out.count("truncated at") == 2


def test_unreadable_file_is_skipped_not_fatal(tmp_path, monkeypatch):
    ws = str(tmp_path)
    _write(project_profile_path(ws), "profile")

    def boom(*_a, **_k):
        raise PermissionError("nope")

    monkeypatch.setattr("builtins.open", boom)
    assert get_whisper_md_context(ws) == ""
