"""WS-D slice 1: rpc framing, journal + resume cache, saved-workflow store,
and the workflow_runs migration."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    yield


# ── rpc ──────────────────────────────────────────────────────────────────────


def test_rpc_roundtrip_and_shapes():
    from server.workflows import rpc

    line = rpc.dumps_line({"id": 3, "method": "agent", "params": {"prompt": "hi"}})
    assert line.endswith("\n")
    msg = rpc.loads_line(line)
    assert rpc.is_request(msg) and msg["method"] == "agent"

    note = rpc.loads_line(rpc.dumps_line({"method": "log", "params": {"message": "x"}}))
    assert rpc.is_notification(note) and not rpc.is_request(note)

    assert rpc.response(3, {"ok": 1}) == {"id": 3, "result": {"ok": 1}}
    err = rpc.error_response(3, rpc.ERR_BUDGET, "over")
    assert err["error"]["type"] == "BudgetExceededError"
    assert rpc.control("cancel") == {"method": "cancel"}


def test_rpc_rejects_oversized_and_malformed():
    from server.workflows import rpc

    with pytest.raises(rpc.RpcError):
        rpc.loads_line("x" * (rpc.MAX_LINE_BYTES + 1))
    with pytest.raises(rpc.RpcError):
        rpc.loads_line("not json")
    with pytest.raises(rpc.RpcError):
        rpc.loads_line("[1,2,3]")  # array, not an object


# ── journal + resume cache ───────────────────────────────────────────────────


def test_call_hash_ignores_label_and_phase():
    from server.workflows.journal import call_hash

    a = call_hash("do X", {"schema": {"type": "object"}, "label": "one", "phase": "p1"})
    b = call_hash("do X", {"schema": {"type": "object"}, "label": "two", "phase": "p2"})
    c = call_hash("do X", {"schema": {"type": "object"}, "effort": "high"})
    assert a == b  # label/phase don't affect the hash
    assert a != c  # a real opt does


def test_journal_write_read_and_resume_cache():
    from server.workflows import journal

    j = journal.Journal("run1")
    j.run_meta({"name": "wf", "phases": ["a"]})
    j.phase("a")
    h = journal.call_hash("prompt1", {"schema": {"type": "object"}})
    j.agent_call(
        {
            "seq": 1,
            "call_hash": h,
            "phase": "a",
            "label": "x",
            "status": "completed",
            "text": "result text",
            "output": {"k": "v"},
            "usage": {"input_tokens": 10},
        }
    )
    j.done({"final": True})

    entries = journal.read_journal("run1")
    types = [e["type"] for e in entries]
    assert types == ["run_meta", "phase", "agent_call", "done"]

    cache = journal.load_resume_cache("run1")
    assert h in cache
    hit = cache[h].popleft()
    assert hit["output"] == {"k": "v"} and hit["status"] == "completed"


def test_resume_cache_fifo_multiset():
    # Two calls with the SAME hash (e.g. parallel identical agents) must both be
    # cached and replay in issue order.
    from server.workflows import journal

    j = journal.Journal("run2")
    h = journal.call_hash("same", {})
    for i in range(2):
        j.agent_call(
            {"seq": i, "call_hash": h, "status": "completed", "text": f"r{i}", "output": i}
        )
    cache = journal.load_resume_cache("run2")
    assert [cache[h].popleft()["output"] for _ in range(2)] == [0, 1]


# ── saved-workflow store ─────────────────────────────────────────────────────


def test_store_save_load_list_trust():
    from server.workflows import store

    assert store.valid_name("fix-and-verify")
    assert not store.valid_name("../evil")
    assert not store.valid_name("Has Spaces")

    meta = {"name": "wf1", "description": "does X", "phases": ["explore", "verify"]}
    stored = store.save_script("wf1", "export const meta = {}\nagent('x')", meta)
    assert stored["script_hash"]
    loaded = store.load_script("wf1")
    assert loaded["trusted"] is False  # untrusted until approved
    assert "export const meta" in loaded["script"]

    listed = store.list_scripts()
    assert any(s["name"] == "wf1" and s["description"] == "does X" for s in listed)

    assert store.approve_script("wf1") is True
    assert store.load_script("wf1")["trusted"] is True

    # Editing the script drops trust (hash mismatch).
    store.save_script("wf1", "export const meta = {}\nagent('y')  // changed", meta, trusted=True)
    # save_script with trusted=True records the NEW hash, so it stays trusted...
    assert store.load_script("wf1")["trusted"] is True
    # ...but an out-of-band edit (hash no longer matches stored) drops it:
    d = store._dir("wf1")
    with open(os.path.join(d, "workflow.mjs"), "w") as f:
        f.write("tampered")
    assert store.load_script("wf1")["trusted"] is False


def test_store_delete():
    from server.workflows import store

    store.save_script("wf2", "x", {"description": "d", "phases": []})
    assert store.load_script("wf2") is not None
    assert store.delete_script("wf2") is True
    assert store.load_script("wf2") is None


# ── migration ────────────────────────────────────────────────────────────────


def test_workflow_runs_migration():
    import importlib
    import sqlite3

    mod = importlib.import_module("server.migrations.009_add_workflow_runs")
    assert mod.VERSION == 9
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    mod.migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(workflow_runs)").fetchall()}
    assert {"run_id", "status", "agents_spawned", "cost_usd", "phases_json"} <= cols
    # status CHECK constraint accepts the documented states, rejects others.
    conn.execute(
        "INSERT INTO workflow_runs (run_id, status, started_at) VALUES ('r1','running','2026')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO workflow_runs (run_id, status, started_at) VALUES ('r2','bogus','2026')"
        )
    conn.close()
