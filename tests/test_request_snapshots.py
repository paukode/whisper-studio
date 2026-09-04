"""Request snapshots: every model round's assembled request is reconstructable
from disk ("model-visible means logged"), best-effort, with bounded retention.
"""

import gzip
import json
import os
import time

import pytest

from server.chat import request_snapshots as snap


class _Adapter:
    provider = "anthropic"
    model_key = "opus5.0"
    model_id = "us.anthropic.claude-opus-5"

    def describe_request(self):
        return {"system_static": "STATIC", "system_dynamic": "DYN", "caching_on": True}


@pytest.fixture
def snap_root(tmp_path, monkeypatch):
    monkeypatch.setattr(snap, "data_root", lambda: str(tmp_path))
    return tmp_path


def _record(session="sess-1", round_num=0, tools=None, messages=None):
    snap.record_request_snapshot(
        session_id=session,
        round_num=round_num,
        adapter=_Adapter(),
        tools=tools if tools is not None else [{"name": "ws_read_file", "input_schema": {}}],
        messages=messages if messages is not None else [{"role": "user", "content": "hi"}],
        effort_label="high",
    )


def test_snapshot_roundtrips_the_full_request(snap_root):
    _record()
    sdir = snap_root / "request_snapshots" / "sess-1"
    files = list(sdir.iterdir())
    assert len(files) == 1
    with gzip.open(files[0], "rt") as f:
        data = json.load(f)
    assert data["round"] == 0
    assert data["model_key"] == "opus5.0"
    assert data["tool_names"] == ["ws_read_file"]
    assert data["tools"][0]["input_schema"] == {}
    assert data["messages"] == [{"role": "user", "content": "hi"}]
    assert data["request_env"]["system_static"] == "STATIC"
    assert data["effort_label"] == "high"


def test_snapshot_never_raises(snap_root, monkeypatch):
    """A snapshot failure must never cost the turn — the write is best-effort."""

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(snap.os, "makedirs", boom)
    _record()  # must not raise


def test_bad_session_id_is_refused(snap_root):
    _record(session="../evil")
    assert not (snap_root / "request_snapshots").exists()


def test_retention_prunes_old_snapshots(snap_root):
    _record(round_num=0)
    sdir = snap_root / "request_snapshots" / "sess-1"
    old = next(iter(sdir.iterdir()))
    stale = time.time() - (snap.RETENTION_DAYS + 1) * 86400
    os.utime(old, (stale, stale))
    _record(round_num=1)
    names = [p.name for p in sdir.iterdir()]
    assert old.name not in names
    assert any("-r1" in n for n in names)


def test_per_session_cap_keeps_newest(snap_root, monkeypatch):
    monkeypatch.setattr(snap, "MAX_PER_SESSION", 3)
    for i in range(5):
        _record(round_num=i)
    sdir = snap_root / "request_snapshots" / "sess-1"
    assert len(list(sdir.iterdir())) <= 3


def test_oversized_messages_are_replaced_not_dropped(snap_root, monkeypatch):
    monkeypatch.setattr(snap, "MAX_SNAPSHOT_BYTES", 1000)
    _record(messages=[{"role": "user", "content": "x" * 5000}])
    sdir = snap_root / "request_snapshots" / "sess-1"
    with gzip.open(next(iter(sdir.iterdir())), "rt") as f:
        data = json.load(f)
    assert isinstance(data["messages"], str) and "omitted" in data["messages"]
    # The envelope survives even when the messages were too big to keep.
    assert data["tool_names"] == ["ws_read_file"]


def test_listing_and_read_back_endpoints(snap_root):
    import asyncio

    _record(round_num=0)
    _record(round_num=1)
    listing = asyncio.run(snap.list_request_snapshots("sess-1"))
    assert len(listing["snapshots"]) == 2
    newest = listing["snapshots"][0]["name"]
    body = asyncio.run(snap.read_request_snapshot("sess-1", newest))
    assert body["session_id"] == "sess-1"

    missing = asyncio.run(snap.read_request_snapshot("sess-1", "nope.json.gz"))
    assert missing.status_code == 404
    traversal = asyncio.run(snap.read_request_snapshot("sess-1", "../../etc/passwd"))
    assert traversal.status_code == 404
