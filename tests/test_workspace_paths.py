"""Unit tests for server/workspace/paths.py — the path-validation and
line-normalisation primitives extracted during the workspace package split.

`_ws_validate_path` is security-critical: it is the guard that keeps tool
file access inside the connected workspace, so its boundary cases (parent
traversal, absolute escape, UNC paths, /dev|/proc|/sys) are pinned here.
"""

import os

from server.workspace.paths import (
    _normalize_lf,
    _strip_trailing_ws,
    _ws_validate_path,
)


def test_validate_path_allows_root_itself(tmp_path):
    root = str(tmp_path)
    assert _ws_validate_path(root, root) is True


def test_validate_path_allows_nested_file(tmp_path):
    root = str(tmp_path)
    nested = os.path.join(root, "src", "main.py")
    assert _ws_validate_path(nested, root) is True


def test_validate_path_blocks_parent_traversal(tmp_path):
    root = str(tmp_path / "ws")
    os.makedirs(root, exist_ok=True)
    escape = os.path.join(root, "..", "secret.txt")
    assert _ws_validate_path(escape, root) is False


def test_validate_path_blocks_sibling_prefix(tmp_path):
    # A sibling dir that shares a string prefix with the root must NOT pass.
    root = str(tmp_path / "ws")
    os.makedirs(root, exist_ok=True)
    sibling = str(tmp_path / "ws-evil" / "f.txt")
    assert _ws_validate_path(sibling, root) is False


def test_validate_path_blocks_unc(tmp_path):
    root = str(tmp_path)
    assert _ws_validate_path(r"\\server\share\x", root) is False
    assert _ws_validate_path("//server/share/x", root) is False


def test_validate_path_blocks_system_dirs(tmp_path):
    root = str(tmp_path)
    for danger in ("/dev/null", "/proc/self/mem", "/sys/kernel"):
        assert _ws_validate_path(danger, root) is False


def test_normalize_lf_collapses_crlf_and_cr():
    assert _normalize_lf("a\r\nb\rc\n") == "a\nb\nc\n"


def test_normalize_lf_noop_on_plain_lf():
    assert _normalize_lf("a\nb\n") == "a\nb\n"


def test_strip_trailing_ws_strips_for_code():
    assert _strip_trailing_ws("a  \nb\t\n", "x.py") == "a\nb\n"


def test_strip_trailing_ws_preserves_markdown():
    # Trailing spaces are significant in Markdown (hard line breaks).
    md = "line one  \nline two  \n"
    assert _strip_trailing_ws(md, "notes.md") == md
    assert _strip_trailing_ws(md, "notes.mdx") == md
