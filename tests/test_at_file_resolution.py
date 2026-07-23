"""Tests for `@file:` / `@file ` mention inlining in server/chat/routes.py.

The chat composer's autocomplete inserts the colon form `@file:<path>` and the
path can contain spaces. The resolver must inline the referenced file's content
into the prompt (so the model doesn't have to hunt for it with tools), handle
both the colon and space forms, tolerate spaces in filenames, preserve trailing
text, and never inline anything outside the validated workspace.
"""

import os

from server.chat.routes import _AT_FILE_INLINE_MAX, _resolve_at_file_mentions


def _write(root, rel, content="hello world"):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full) or root, exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    return full


def test_colon_form_inlines_content(tmp_path):
    ws = str(tmp_path)
    _write(ws, "notes.txt", "the body")
    out = _resolve_at_file_mentions("@file:notes.txt", ws)
    assert "[File: notes.txt]" in out
    assert "the body" in out
    assert "@file:notes.txt" not in out


def test_space_form_still_works(tmp_path):
    ws = str(tmp_path)
    _write(ws, "notes.txt", "the body")
    out = _resolve_at_file_mentions("@file notes.txt", ws)
    assert "[File: notes.txt]" in out
    assert "the body" in out


def test_filename_with_spaces_resolves(tmp_path):
    ws = str(tmp_path)
    _write(ws, "console output.log", "LOG CONTENT")
    out = _resolve_at_file_mentions("cat @file:console output.log", ws)
    assert out.startswith("cat ")
    assert "[File: console output.log]" in out
    assert "LOG CONTENT" in out


def test_trailing_text_preserved(tmp_path):
    ws = str(tmp_path)
    _write(ws, "notes.txt", "X")
    out = _resolve_at_file_mentions("cat @file:notes.txt please summarize", ws)
    assert "[File: notes.txt]" in out
    assert out.rstrip().endswith("please summarize")


def test_multiple_mentions_both_inline(tmp_path):
    ws = str(tmp_path)
    _write(ws, "a.txt", "AAA")
    _write(ws, "b.txt", "BBB")
    out = _resolve_at_file_mentions("compare @file:a.txt and @file:b.txt", ws)
    assert "[File: a.txt]" in out and "AAA" in out
    assert "[File: b.txt]" in out and "BBB" in out
    assert "and" in out  # surrounding text preserved


def test_unresolved_mention_left_verbatim(tmp_path):
    ws = str(tmp_path)
    out = _resolve_at_file_mentions("show @file:does_not_exist.txt", ws)
    assert "@file:does_not_exist.txt" in out
    assert "[File:" not in out


def test_path_traversal_not_inlined(tmp_path):
    # A file outside the workspace must never be inlined.
    ws = tmp_path / "ws"
    ws.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    out = _resolve_at_file_mentions("@file:../secret.txt", str(ws))
    assert "TOP SECRET" not in out
    assert "[File:" not in out
    assert "../secret.txt" in out  # left verbatim


def test_oversize_file_truncated(tmp_path):
    ws = str(tmp_path)
    big = "x" * (_AT_FILE_INLINE_MAX + 5000)
    _write(ws, "big.log", big)
    out = _resolve_at_file_mentions("@file:big.log", ws)
    assert "... (truncated)" in out
    assert len(out) < len(big)
