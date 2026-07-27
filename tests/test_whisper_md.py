"""WHISPER.md loads as a tree: root always, directory-scoped on demand."""

import os

from server.whisper_md import (
    ROOT_MAX_CHARS,
    SCOPED_MAX_CHARS,
    find_scoped_files,
    get_whisper_md_context,
)


def _write(base, rel, text):
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


def test_no_workspace_and_no_file_yield_nothing(tmp_path):
    assert get_whisper_md_context(None) == ""
    assert get_whisper_md_context(str(tmp_path)) == ""


def test_root_file_is_always_inlined(tmp_path):
    _write(str(tmp_path), "WHISPER.md", "Root rules here.")
    out = get_whisper_md_context(str(tmp_path), "anything at all")
    assert "Root rules here." in out


def test_scoped_file_loads_only_when_the_turn_concerns_its_directory(tmp_path):
    ws = str(tmp_path)
    _write(ws, "WHISPER.md", "Root rules.")
    _write(ws, "server/auth/WHISPER.md", "AUTH GOTCHA")
    _write(ws, "src/components/WHISPER.md", "COMPONENT GOTCHA")

    on_topic = get_whisper_md_context(ws, "fix the login in server/auth")
    assert "AUTH GOTCHA" in on_topic
    # The other one is announced by path, not inlined.
    assert "COMPONENT GOTCHA" not in on_topic
    assert "src/components/WHISPER.md" in on_topic

    off_topic = get_whisper_md_context(ws, "what is the weather today")
    assert "AUTH GOTCHA" not in off_topic
    assert "COMPONENT GOTCHA" not in off_topic
    assert "server/auth/WHISPER.md" in off_topic
    assert "Root rules." in off_topic


def test_a_bare_directory_name_is_enough_to_match(tmp_path):
    ws = str(tmp_path)
    _write(ws, "server/diarization/WHISPER.md", "SPEAKER RULES")
    assert "SPEAKER RULES" in get_whisper_md_context(ws, "improve diarization accuracy")


def test_discovery_skips_vendor_directories(tmp_path):
    ws = str(tmp_path)
    _write(ws, "app/WHISPER.md", "real")
    _write(ws, "node_modules/pkg/WHISPER.md", "vendored")
    _write(ws, ".venv/lib/WHISPER.md", "vendored")

    found = find_scoped_files(ws)
    assert found == ["app/WHISPER.md"]


def test_oversized_files_are_capped_with_a_pointer_to_the_rest(tmp_path):
    """An oversized file is capped, never inlined verbatim on every request."""
    ws = str(tmp_path)
    _write(ws, "WHISPER.md", "x" * (ROOT_MAX_CHARS * 3))
    _write(ws, "api/WHISPER.md", "y" * (SCOPED_MAX_CHARS * 3))

    out = get_whisper_md_context(ws, "change the api")
    assert len(out) < ROOT_MAX_CHARS + SCOPED_MAX_CHARS + 2000
    assert out.count("truncated at") == 2
    assert "ws_read_file" in out


def test_unreadable_file_is_skipped_not_fatal(tmp_path, monkeypatch):
    ws = str(tmp_path)
    _write(ws, "WHISPER.md", "root")

    def boom(*_a, **_k):
        raise PermissionError("nope")

    monkeypatch.setattr("builtins.open", boom)
    assert get_whisper_md_context(ws, "hi") == ""
