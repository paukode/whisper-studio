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


def _read_all(sdir):
    out = []
    for p in sorted(sdir.iterdir()):
        with gzip.open(p, "rt") as f:
            out.append(json.load(f))
    return sorted(out, key=lambda s: s["ts"])


def test_local_snapshots_hold_the_exact_bodies_posted_including_the_retry(snap_root, monkeypatch):
    """The local adapter reshapes the request (OpenAI chat messages, roles
    merged, an empty assistant row dropped) and may retry within a round. The
    snapshots must hold what llama-server received on each request."""
    import asyncio

    import server.local.server_stream as SS
    from server.chat.engine.local import LocalAdapter
    from server.chat.engine.policy import LOCAL_POLICY
    from server.chat.engine.runner import TurnContext, run_turn

    posted: list[dict] = []
    replies = iter([[], [("text", "recovered")]])  # empty completion, then an answer

    async def fake_stream(base_url, payload):
        posted.append(payload)
        for piece in next(replies):
            yield piece
        yield ("done", {"calls": [], "finish_reason": "stop", "usage": {}})

    monkeypatch.setattr(SS, "_stream_round", fake_stream)
    history = [
        {"role": "user", "content": "hey!"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "second"},
    ]
    ctx = TurnContext(
        cost_source="chat",
        session_id="snap-local",
        model_key="local_gemma",
        model_id="local_gemma",
        messages=list(history),
        adapter=LocalAdapter(
            model_key="local_gemma",
            base_url="http://x",
            system_prompt="sys",
            thinking=True,
            tools_enabled=False,
        ),
        policy=LOCAL_POLICY,
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
    )

    async def go():
        return [c async for c in run_turn(ctx)]

    asyncio.run(go())
    snaps = _read_all(snap_root / "request_snapshots" / "snap-local")
    assert len(posted) == 2
    assert [s["attempt"] for s in snaps] == [0, 1]
    assert [s["wire_request"] for s in snaps] == posted
    # The canonical history still rides along, so the reshaping is visible.
    assert {"role": "assistant", "content": ""} in snaps[0]["messages"]
    assert all(m["role"] != "assistant" for m in snaps[0]["wire_request"]["messages"])


def test_a_wire_body_that_cannot_be_built_is_recorded_as_the_finding(snap_root):
    def boom():
        raise TypeError("can only join str")

    snap.record_request_snapshot(
        session_id="sess-1", round_num=0, adapter=_Adapter(), tools=[], messages=[], wire=boom
    )
    (data,) = _read_all(snap_root / "request_snapshots" / "sess-1")
    assert "can only join str" in data["wire_request"]["error"]


def test_oversized_wire_messages_are_replaced_like_the_canonical_ones(snap_root, monkeypatch):
    monkeypatch.setattr(snap, "MAX_SNAPSHOT_BYTES", 1000)
    snap.record_request_snapshot(
        session_id="sess-1",
        round_num=0,
        adapter=_Adapter(),
        tools=[],
        messages=[{"role": "user", "content": "x" * 5000}],
        wire={"model": "m", "messages": [{"role": "user", "content": "x" * 5000}]},
    )
    (data,) = _read_all(snap_root / "request_snapshots" / "sess-1")
    assert data["wire_request"]["model"] == "m"
    assert "omitted" in data["wire_request"]["messages"]
