"""The /api/workspace/connect response must include a `writable` hint so the
frontend can warn the user about read-only folders without refusing the
connection. The hint is best-effort (os.access) — see _check_writable.
"""

import os
import stat
import tempfile

from server.workspace import _check_writable


def test_check_writable_true_for_normal_dir():
    with tempfile.TemporaryDirectory() as d:
        assert _check_writable(d) is True


def test_check_writable_false_for_nonexistent_path():
    assert _check_writable("/nonexistent/path/does/not/exist") is False


def test_check_writable_false_for_a_file_path():
    """Files aren't directories — the helper should reject them up front."""
    with tempfile.NamedTemporaryFile() as f:
        assert _check_writable(f.name) is False


def test_check_writable_false_for_readonly_dir():
    """Mode 0o555 strips write — the check should notice."""
    if os.geteuid() == 0:
        # Root bypasses W_OK, so this assertion would fail under sudo. Skip.
        import pytest

        pytest.skip("running as root; os.access ignores W_OK denial")

    with tempfile.TemporaryDirectory() as d:
        os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)  # 0o500
        try:
            assert _check_writable(d) is False
        finally:
            # Restore permissive mode so TemporaryDirectory can clean up.
            os.chmod(d, stat.S_IRWXU)


def test_check_writable_does_not_raise_on_oserror():
    """If os.access throws (e.g. unreachable network mount surfaces ENXIO),
    we report False rather than letting the connect endpoint 500."""
    # An effectively-impossible path that triggers OS-level errors on some
    # platforms; the helper must swallow them.
    assert _check_writable("\0invalid\0path") is False
