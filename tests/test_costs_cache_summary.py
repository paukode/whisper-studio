"""Cache telemetry aggregation and the summary endpoint's context fields."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.costs.tracker as tracker
from server.chat import loop_hints


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", str(tmp_path / "costs.db"))
    # The canonical table comes from migration 001; create the same shape here.
    with tracker._get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                turn_number INTEGER NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                api_duration_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
    with loop_hints._lock:
        loop_hints._context.clear()
    yield


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(tracker.router)
    return TestClient(app)


def _record(model="opus4.8", inp=1000, out=100, read=0, write=0, session="s1"):
    tracker.record_turn(
        session_id=session,
        turn_number=0,
        model=model,
        input_tokens=inp,
        output_tokens=out,
        cost_usd=tracker.estimate_cost(model, inp, out, read, write),
        cache_read_tokens=read,
        cache_creation_tokens=write,
    )


def test_cache_stats_math():
    _record(inp=1000, read=9000, write=2000)
    _record(inp=500, read=4000, write=0)
    stats = tracker.get_cache_stats()
    assert stats["read_tokens"] == 13_000
    assert stats["write_tokens"] == 2_000
    total = 13_000 + 2_000 + 1_500
    assert stats["hit_rate"] == round(13_000 / total, 4)
    pricing = tracker._resolve_pricing("opus4.8")
    if pricing:
        expected = (13_000 / 1_000_000) * (pricing["input"] - pricing["cache_read"])
        assert stats["est_savings_usd"] == round(expected, 4)


def test_cache_stats_empty():
    stats = tracker.get_cache_stats()
    assert stats == {
        "read_tokens": 0,
        "write_tokens": 0,
        "hit_rate": 0.0,
        "est_savings_usd": 0.0,
    }


def test_summary_includes_cache_block(client):
    _record(read=5000, write=1000)
    data = client.get("/api/costs/summary").json()
    assert "cache" in data
    assert data["cache"]["read_tokens"] == 5000
    assert data["cache"]["write_tokens"] == 1000
    assert "context_used" not in data  # no session_id: no context fields


def test_summary_with_session_id_populates_context(client):
    loop_hints.note_prompt_tokens("sess-x", 88_000, 200_000)
    data = client.get("/api/costs/summary?session_id=sess-x").json()
    assert data["context_used"] == 88_000
    assert data["context_max"] == 200_000
    # Unknown session: fields simply absent, bar stays dormant.
    data2 = client.get("/api/costs/summary?session_id=never-seen").json()
    assert "context_used" not in data2


def test_session_summary_includes_cache_creation():
    _record(read=100, write=700, session="s9")
    summary = tracker.get_session_summary("s9")
    assert summary["total_cache_creation"] == 700


def test_cache_stats_normalizes_openai_inclusive_convention():
    """GPT rows record input INCLUSIVE of cached tokens; the hit rate must not
    double-count them (100K input with 80K cached is an 80% hit rate)."""
    _record(model="gpt5.6-sol", inp=100_000, read=80_000, write=0)
    stats = tracker.get_cache_stats()
    assert stats["read_tokens"] == 80_000
    # denominator = 80K read + 20K uncached remainder = 100K -> 0.8
    assert stats["hit_rate"] == 0.8


def test_pricing_resolves_longest_prefix():
    """sonnet5_subagent must resolve to sonnet5, never the shorter sonnet key."""
    assert tracker._resolve_pricing("sonnet5_subagent") is tracker._MODEL_PRICING["sonnet5"]
    assert tracker._resolve_pricing("sonnet_subagent") is tracker._MODEL_PRICING["sonnet"]
    assert tracker._resolve_pricing("unknown-model") is None
