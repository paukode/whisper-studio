"""Verify workspace file writes are atomic and preserve permissions."""

import os
import stat
import tempfile

from server.workspace import _atomic_write_text
from server.workspace.paths import _atomic_write_bytes


def test_creates_new_file_with_parents():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "a", "b", "c.txt")
        _atomic_write_text(target, "hello world")
        assert open(target).read() == "hello world"


def test_overwrite_preserves_existing_mode():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "x.txt")
        with open(target, "w") as f:
            f.write("original")
        os.chmod(target, 0o640)
        _atomic_write_text(target, "rewritten")
        assert open(target).read() == "rewritten"
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o640


def test_does_not_leave_temp_file_on_success():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "y.txt")
        _atomic_write_text(target, "ok")
        siblings = [f for f in os.listdir(d) if f.startswith(".y.txt.tmp.")]
        assert siblings == []


# ── _atomic_write_bytes (save_file's binary counterpart) ──────────────


def test_bytes_creates_new_file_with_parents():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "a", "b", "c.bin")
        data = bytes(range(256))
        _atomic_write_bytes(target, data)
        with open(target, "rb") as f:
            assert f.read() == data


def test_bytes_overwrite_preserves_existing_mode():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "x.bin")
        with open(target, "wb") as f:
            f.write(b"original")
        os.chmod(target, 0o640)
        _atomic_write_bytes(target, b"rewritten")
        with open(target, "rb") as f:
            assert f.read() == b"rewritten"
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o640


def test_bytes_does_not_leave_temp_file_on_success():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "y.bin")
        _atomic_write_bytes(target, b"ok")
        siblings = [f for f in os.listdir(d) if f.startswith(".y.bin.tmp.")]
        assert siblings == []


def test_bytes_does_not_normalize_line_endings():
    # Binary content must round-trip byte-for-byte — no \r\n -> \n rewrite
    # (that's what _atomic_write_text's _normalize_lf pass does; this
    # helper deliberately skips it).
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "crlf.bin")
        data = b"line1\r\nline2\r\n"
        _atomic_write_bytes(target, data)
        with open(target, "rb") as f:
            assert f.read() == data
