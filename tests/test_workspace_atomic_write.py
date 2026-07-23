"""Verify workspace file writes are atomic and preserve permissions."""

import os
import stat
import tempfile

from server.workspace import _atomic_write_text


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
