"""The result-cache store: write/read roundtrips, filename safety, 7-day
retention GC, and the HTTP surface the truncation toast links to."""

import os
import time

import pytest

from server.executors.result_cache import _exec_read_cached_result
from server.infrastructure import result_cache


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path))


def test_write_read_roundtrip():
    fname = result_cache.write("aws_boto3", "line one\nline two\nline three")
    assert fname.startswith("aws_boto3_") and fname.endswith(".txt")
    out = result_cache.read(fname)
    assert "    1\tline one" in out
    assert "    3\tline three" in out


def test_read_offset_and_limit():
    fname = result_cache.write("tool", "\n".join(f"row {i}" for i in range(1, 101)))
    out = result_cache.read(fname, offset=50, limit=2)
    assert "   50\trow 50" in out
    assert "   51\trow 51" in out
    assert "row 52" not in out
    assert "row 49" not in out


def test_tool_name_sanitized_in_filename():
    fname = result_cache.write("weird/../tool name!", "data")
    assert result_cache.is_safe_filename(fname)
    assert "/" not in fname and ".." not in fname.replace(".txt", "")


@pytest.mark.parametrize(
    "bad",
    [
        "../etc/passwd",
        "sub/dir.txt",
        "/etc/passwd",
        "..",
        "",
        ".hidden.txt",
        "no_extension",
    ],
)
def test_unsafe_filenames_rejected(bad):
    assert result_cache.is_safe_filename(bad) is False
    assert result_cache.full_path(bad) is None
    assert "error" in result_cache.read(bad).lower() or "invalid" in result_cache.read(bad).lower()


def test_read_missing_file_mentions_retention():
    out = result_cache.read("tool_123.txt")
    assert "not found" in out.lower()
    assert "7-day" in out or str(result_cache.RETENTION_DAYS) in out


def test_gc_removes_old_keeps_fresh():
    old = result_cache.write("old_tool", "stale")
    old_path = os.path.join(result_cache.cache_dir(), old)
    eight_days_ago = time.time() - 8 * 86400
    os.utime(old_path, (eight_days_ago, eight_days_ago))

    fresh = result_cache.write("fresh_tool", "new")  # GC runs on write
    assert not os.path.exists(old_path)
    assert os.path.exists(os.path.join(result_cache.cache_dir(), fresh))


def test_read_caps_output_under_budget():
    # A huge cached file must come back under the chat budgeter's 50KB so it
    # never re-triggers truncation (cache-of-a-cache).
    fname = result_cache.write("huge", "\n".join("y" * 100 for _ in range(2000)))
    out = result_cache.read(fname)
    assert len(out.encode()) <= 50_000
    assert "capped" in out


def test_executor_wraps_read():
    fname = result_cache.write("tool", "alpha\nbeta")
    out = _exec_read_cached_result({"filename": fname, "__session_id__": "s1"}, "", {})
    assert "alpha" in out and "beta" in out
    # Bad offset/limit types degrade to defaults instead of crashing.
    out2 = _exec_read_cached_result({"filename": fname, "offset": "x", "limit": "y"}, "", {})
    assert "alpha" in out2


def test_http_endpoint_serves_and_404s():
    fname = result_cache.write("tool", "full output body")
    ok = result_cache.get_cached_result(fname)
    assert ok.status_code == 200
    assert b"full output body" in ok.body

    missing = result_cache.get_cached_result("tool_999.txt")
    assert missing.status_code == 404

    traversal = result_cache.get_cached_result("..%2Fconfig.json")
    assert traversal.status_code == 404
