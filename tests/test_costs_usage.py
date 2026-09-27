"""The cost log read back: priced at read time, UTC days, the usage contract.

GET /api/costs/usage replaces /summary, /models and /daily; every figure is
the stored token counts priced with the current rate table.
"""

import csv
import importlib
import io
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.costs import tracker, usage
from server.costs.routes import router

m022 = importlib.import_module("server.migrations.022_cost_rows_priced_at_read_time")


def _row(
    session="s1",
    model="opus5.0",
    inp=0,
    out=0,
    read=0,
    write=0,
    source="chat",
    at="2026-09-10 12:00:00",
    estimated=(),
):
    tracker.record_turn(
        session,
        0,
        model,
        inp,
        out,
        cache_read_tokens=read,
        cache_creation_tokens=write,
        source=source,
        estimated=estimated,
    )
    with tracker._get_conn() as conn:
        conn.execute(
            "UPDATE session_costs SET created_at = ? WHERE id = (SELECT MAX(id) FROM session_costs)",
            (at,),
        )


def _cost(model, inp=0, out=0, read=0, write=0):
    return tracker.estimate_cost(model, inp, out, read, write)


def _report(start, end, granularity="day", split="model"):
    return usage.usage_report(
        date.fromisoformat(start), date.fromisoformat(end), granularity, split
    )


def _seed_spread():
    _row(session="a", model="opus5.0", inp=100, out=50, read=4000, at="2026-08-30 09:00:00")
    _row(session="a", model="gpt6-astra", inp=9000, out=300, read=8000, at="2026-09-01 10:00:00")
    _row(session="b", model="haiku", inp=700, out=20, source="title", at="2026-09-07 23:59:59")
    _row(session="b", model="sonnet5", inp=5, out=900, read=60_000, write=2000, at="2026-09-15")
    _row(
        session="c", model="gpt5.6-sol", inp=1000, out=10, read=900, source="agent", at="2026-09-15"
    )


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── bucketing ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("granularity", ["day", "week", "month"])
def test_buckets_sum_to_the_range_totals(granularity):
    _seed_spread()
    rep = _report("2026-08-29", "2026-09-16", granularity)
    total = rep["totals"]["cost_usd"]
    assert total > 0
    assert sum(b["cost_usd"] for b in rep["buckets"]) == pytest.approx(total)
    for b in rep["buckets"]:
        assert sum(v["cost_usd"] for v in b["by_key"].values()) == pytest.approx(b["cost_usd"])
    calls = sum(v["calls"] for b in rep["buckets"] for v in b["by_key"].values())
    assert calls == rep["totals"]["calls"] == 5


def test_day_buckets_are_zero_filled_over_the_whole_range():
    _row(at="2026-09-03 08:00:00", inp=10, out=10)
    rep = _report("2026-09-01", "2026-09-05")
    assert [b["start"] for b in rep["buckets"]] == [f"2026-09-0{d}" for d in range(1, 6)]
    assert [bool(b["by_key"]) for b in rep["buckets"]] == [False, False, True, False, False]
    assert all(b["start"] == b["end"] and not b["partial"] for b in rep["buckets"])


def test_weeks_start_on_monday_and_clipped_buckets_are_partial():
    # 2026-09-02 is a Wednesday, 2026-09-20 a Sunday.
    rep = _report("2026-09-02", "2026-09-20", "week")
    spans = [(b["start"], b["end"], b["partial"]) for b in rep["buckets"]]
    assert spans == [
        ("2026-09-02", "2026-09-06", True),
        ("2026-09-07", "2026-09-13", False),
        ("2026-09-14", "2026-09-20", False),
    ]
    assert all(date.fromisoformat(s).weekday() == 0 for s, _e, p in spans if not p)
    months = _report("2026-08-30", "2026-09-15", "month")["buckets"]
    assert [(b["start"], b["end"], b["partial"]) for b in months] == [
        ("2026-08-30", "2026-08-31", True),
        ("2026-09-01", "2026-09-15", True),
    ]


def test_days_are_utc_days():
    _row(inp=100, at="2026-09-10 23:59:59")
    _row(inp=100, at="2026-09-11 00:00:00")
    rep = _report("2026-09-10", "2026-09-11")
    assert [sum(v["calls"] for v in b["by_key"].values()) for b in rep["buckets"]] == [1, 1]
    assert rep["range"]["timezone"] == "UTC"
    assert _report("2026-09-10", "2026-09-10")["totals"]["calls"] == 1


# ── splits, normalization, notes ─────────────────────────────────────────────


@pytest.mark.parametrize("split", ["model", "session", "source"])
def test_every_split_adds_up_to_the_totals(split):
    _seed_spread()
    rep = _report("2026-08-01", "2026-09-30", "week", split)
    rows, totals = rep["rows"], rep["totals"]
    assert sum(r["cost_usd"] for r in rows) == pytest.approx(totals["cost_usd"])
    assert sum(r["calls"] for r in rows) == totals["calls"]
    assert sum(r["prompt_tokens"] for r in rows) == totals["prompt_tokens"]
    assert sum(r["share"] for r in rows) == pytest.approx(1.0)
    assert [r["cost_usd"] for r in rows] == sorted((r["cost_usd"] for r in rows), reverse=True)


def test_prompt_tokens_are_normalized_so_gpt_and_claude_add_up():
    # Claude: disjoint buckets (4 + 1000 read + 100 write). GPT: the cached
    # 800 are part of its 1000 input.
    _row(model="opus5.0", inp=4, read=1000, write=100)
    _row(model="gpt6-astra", inp=1000, read=800)
    totals = _report("2026-09-10", "2026-09-10")["totals"]
    assert totals["prompt_tokens"] == 1104 + 1000
    assert totals["cache_read_tokens"] == 1800
    assert totals["cache_hit_rate"] == pytest.approx(1800 / 2104, abs=1e-4)


def test_cache_savings_net_the_write_premium():
    _row(model="opus5.0", read=1_000_000, write=1_000_000)
    rates = tracker.get_model_pricing("opus5.0")
    expected = (rates["input"] - rates["cache_read"]) - (rates["cache_write"] - rates["input"])
    assert _report("2026-09-10", "2026-09-10")["totals"]["cache_savings_usd"] == pytest.approx(
        expected
    )


def test_gpt_figures_carry_the_dated_list_rate_note():
    _row(session="mix", model="gpt6-astra", inp=1000, out=10)
    _row(session="mix", model="opus5.0", inp=1000, out=10)
    _row(session="claude-only", model="opus5.0", inp=1000, out=10)
    by_model = {r["key"]: r for r in _report("2026-09-10", "2026-09-10")["rows"]}
    assert by_model["gpt6-astra"]["note"] == usage.GPT_NOTE
    assert "$0" in usage.GPT_NOTE and "2026-09-23" in usage.GPT_NOTE
    assert by_model["opus5.0"]["note"] == ""
    by_session = {r["key"]: r for r in _report("2026-09-10", "2026-09-10", split="session")["rows"]}
    assert by_session["mix"]["note"] == usage.GPT_MIXED_NOTE
    assert by_session["claude-only"]["note"] == ""


def test_an_unpriced_model_says_why_it_shows_zero():
    _row(model="retired-model", inp=1000, out=10)
    rep = _report("2026-09-10", "2026-09-10")
    (row,) = rep["rows"]
    assert row["cost_usd"] == 0 and row["note"] == usage.UNPRICED_NOTE
    assert rep["totals"]["unpriced_calls"] == 1


def test_unpriced_calls_count_billed_models_only():
    # On-device calls are free by design; a billed model with no rate is not.
    _row(model="local_qwen3", inp=500, out=10)
    _row(model="cohere.rerank-v3-5:0", inp=300)
    _row(model="cohere.rerank-v3-5:0", inp=200)
    _row(model="opus5.0", inp=100, out=5)
    totals = _report("2026-09-10", "2026-09-10")["totals"]
    assert (totals["calls"], totals["unpriced_calls"]) == (4, 2)


def test_session_rows_outlive_their_session():
    with tracker._get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("s-live", "Quarterly numbers", "2026-09-10", "2026-09-10"),
        )
    _row(session="s-live", inp=10)
    _row(session="s-gone", inp=10)
    _row(session="", inp=10, source="index")
    rows = {r["key"]: r for r in _report("2026-09-10", "2026-09-10", split="session")["rows"]}
    assert (rows["s-live"]["label"], rows["s-live"]["deleted"]) == ("Quarterly numbers", False)
    assert (rows["s-gone"]["label"], rows["s-gone"]["deleted"]) == ("Deleted session", True)
    assert (rows[""]["label"], rows[""]["deleted"]) == ("No session", False)


def test_source_rows_are_labelled_and_estimates_counted():
    _row(source="title", model="haiku", inp=100, out=5)
    _row(source="chat", model="gpt6-astra", inp=250, out=40, estimated=("input", "output"))
    rep = _report("2026-09-10", "2026-09-10", split="source")
    rows = {r["key"]: r for r in rep["rows"]}
    assert rows["title"]["label"] == "Titles"
    assert rows["chat"]["estimated_calls"] == 1 and rows["title"]["estimated_calls"] == 0
    assert rep["totals"]["estimated_calls"] == 1


# ── read-time pricing ────────────────────────────────────────────────────────


def test_a_rate_change_reprices_history_everywhere(monkeypatch):
    _row(session="rp", model="opus5.0", inp=1000, out=1000, at="2026-09-10 10:00:00")
    before = _report("2026-09-10", "2026-09-10")["totals"]["cost_usd"]
    session_before = tracker.get_session_usage("rp")["cost_usd"]
    export_before = usage.export_records(date(2026, 9, 10), date(2026, 9, 10))[0]["cost_usd"]
    assert before == pytest.approx(session_before) == pytest.approx(export_before)

    doubled = {
        k: v * 2 if isinstance(v, float) else v
        for k, v in tracker._MODEL_PRICING["opus5.0"].items()
    }
    monkeypatch.setitem(tracker._MODEL_PRICING, "opus5.0", doubled)

    assert _report("2026-09-10", "2026-09-10")["totals"]["cost_usd"] == pytest.approx(2 * before)
    assert tracker.get_session_usage("rp")["cost_usd"] == pytest.approx(2 * before)
    exported = usage.export_records(date(2026, 9, 10), date(2026, 9, 10))[0]["cost_usd"]
    assert exported == pytest.approx(2 * before)


def test_every_reader_prices_each_call_at_its_own_long_context_tier():
    # Two GPT-5.6 Sol calls on one day: one over the 272K break, one under.
    # Summed they are far over it; priced per call only the first is long.
    now = datetime.now(timezone.utc)
    at = now.strftime("%Y-%m-%d 00:00:01")
    long_call = dict(inp=287_513, out=152, read=286_454)
    short_call = dict(inp=250_001, out=54, read=249_935)
    _row(session="lc", model="gpt5.6-sol", at=at, **long_call)
    _row(session="lc", model="gpt5.6-sol", at=at, **short_call)
    each = [
        tracker.call_cost("gpt5.6-sol", c["inp"], c["out"], c["read"])
        for c in (long_call, short_call)
    ]
    assert tracker.is_long_context("gpt5.6-sol", long_call["inp"])
    assert not tracker.is_long_context("gpt5.6-sol", short_call["inp"])
    expected = sum(each)
    day = now.date()
    rep = usage.usage_report(day, day, "day", "model")
    assert rep["totals"]["cost_usd"] == pytest.approx(expected, abs=1e-6)
    assert rep["buckets"][0]["cost_usd"] == pytest.approx(expected, abs=1e-6)
    assert rep["rows"][0]["cost_usd"] == pytest.approx(expected, abs=1e-6)
    assert tracker.get_session_usage("lc")["cost_usd"] == pytest.approx(expected, abs=1e-6)
    assert usage.today_spend_usd() == pytest.approx(expected)
    exported = [r["cost_usd"] for r in usage.export_records(day, day)]
    assert exported == [pytest.approx(c, abs=1e-6) for c in each]
    # Pricing the day's summed counts would bill the short call long too.
    summed = tracker.estimate_cost(
        "gpt5.6-sol",
        long_call["inp"] + short_call["inp"],
        long_call["out"] + short_call["out"],
        long_call["read"] + short_call["read"],
        long_context=True,
    )
    assert summed > expected


def test_the_daily_cap_reads_the_current_utc_day():
    now = datetime.now(timezone.utc)
    _row(model="opus5.0", inp=1_000_000, at=now.strftime("%Y-%m-%d 00:00:01"))
    _row(model="opus5.0", inp=1_000_000, at=(now - timedelta(days=1)).strftime("%Y-%m-%d 23:59:59"))
    assert usage.today_spend_usd() == pytest.approx(_cost("opus5.0", inp=1_000_000))


def test_the_session_readout_keeps_its_shape():
    _row(session="shape", model="opus5.0", inp=4, out=50, read=9000, write=1000)
    assert set(tracker.get_session_usage("shape")) == {
        "prompt_tokens",
        "output_tokens",
        "cost_usd",
        "rounds",
    }
    assert tracker.get_session_usage("shape")["prompt_tokens"] == 10_004


def test_the_session_readout_carries_the_gpt_note_and_estimates(client):
    # 257 GPT-6 Astra rounds and 7 Opus 5 rounds: the composer shows the
    # session's figure with the dated GPT note, as the Costs tab row does.
    for _ in range(3):
        _row(session="mixed", model="gpt6-astra", inp=180_000, out=1000, read=170_000)
    _row(session="mixed", model="opus5.0", inp=20, out=40, read=9000)
    _row(session="mixed", model="gpt6-astra", inp=500, out=20, estimated=("input", "output"))
    _row(session="gpt-only", model="gpt5.6-sol", inp=1000, out=10)
    _row(session="claude-only", model="opus5.0", inp=1000, out=10)

    mixed = client.get("/api/costs/session/mixed").json()
    assert (mixed["rounds"], mixed["gpt_rounds"], mixed["estimated_rounds"]) == (5, 4, 1)
    assert mixed["note"] == usage.GPT_MIXED_NOTE
    assert mixed["cost_usd"] == pytest.approx(tracker.get_session_usage("mixed")["cost_usd"])
    assert client.get("/api/costs/session/gpt-only").json()["note"] == usage.GPT_NOTE
    claude = client.get("/api/costs/session/claude-only").json()
    assert (claude["gpt_rounds"], claude["note"]) == (0, "")


@pytest.fixture
def unreadable_log(monkeypatch):
    """Every cost-log read fails, as a locked or corrupt sessions.db would."""
    from contextlib import contextmanager

    @contextmanager
    def broken():
        raise sqlite3.OperationalError("database disk image is malformed")
        yield

    monkeypatch.setattr(tracker, "_get_conn", broken)


def test_an_unreadable_log_is_an_error_never_an_empty_report(unreadable_log, client):
    with pytest.raises(tracker.CostLogUnreadable):
        _report("2026-09-01", "2026-09-02")
    with pytest.raises(tracker.CostLogUnreadable):
        usage.today_spend_usd()
    with pytest.raises(tracker.CostLogUnreadable):
        tracker.get_session_usage("s1")
    for path in (
        "/api/costs/usage?from=2026-09-01&to=2026-09-02",
        "/api/costs/usage",
        "/api/costs/export?from=2026-09-01&to=2026-09-02&format=csv",
        "/api/costs/session/s1",
    ):
        r = client.get(path)
        assert r.status_code == 503, path
        assert "malformed" in r.json()["detail"]


@pytest.mark.parametrize(
    "cap, kind", [("max_session_cost_usd", "session"), ("max_daily_cost_usd", "daily")]
)
def test_a_cap_whose_spend_cannot_be_read_stops_the_run_with_the_reason(
    unreadable_log, monkeypatch, cap, kind
):
    from server.costs import budget

    monkeypatch.setattr(budget, "load_config", lambda: {cap: 5.0})
    for exceeded in (budget.check_budget("s1"), budget.check_budget_soft("s1", 0.9)):
        assert exceeded is not None and exceeded.kind == kind
        assert "malformed" in exceeded.message and cap in exceeded.message


# ── HTTP contract ────────────────────────────────────────────────────────────


def test_usage_endpoint_matches_the_contract_shape(client):
    _seed_spread()
    data = client.get(
        "/api/costs/usage?from=2026-08-29&to=2026-09-16&granularity=week&split=model"
    ).json()
    assert set(data) == {"range", "granularity", "split", "totals", "buckets", "rows"}
    assert data["range"] == {"from": "2026-08-29", "to": "2026-09-16", "timezone": "UTC"}
    assert set(data["totals"]) == {
        "cost_usd",
        "prompt_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "output_tokens",
        "calls",
        "estimated_calls",
        "unpriced_calls",
        "cache_hit_rate",
        "cache_savings_usd",
    }
    for b in data["buckets"]:
        assert set(b) == {"start", "end", "partial", "cost_usd", "by_key"}
        for v in b["by_key"].values():
            assert set(v) == {"cost_usd", "prompt_tokens", "output_tokens", "calls"}
    for r in data["rows"]:
        assert set(r) == {
            "key",
            "label",
            "cost_usd",
            "share",
            "prompt_tokens",
            "cached_pct",
            "output_tokens",
            "calls",
            "estimated_calls",
            "note",
            "deleted",
        }
        assert 0.0 <= r["share"] <= 1.0 and 0.0 <= r["cached_pct"] <= 100.0


def test_cached_pct_is_a_percent_while_share_and_hit_rate_are_fractions():
    # The frontend contract fixture's numbers (src/test/fixtures/costs-usage.json):
    # Opus 5 at 1,000,000 prompt tokens is 75.0 percent cached, GPT-6 Astra
    # at 4,000,000 (its cached tokens a subset of its input) 87.5 percent.
    _row(model="opus5.0", inp=250_000, read=750_000)
    _row(model="gpt6-astra", inp=4_000_000, read=3_500_000)
    rep = _report("2026-09-10", "2026-09-10")
    rows = {r["key"]: r for r in rep["rows"]}
    assert (rows["opus5.0"]["prompt_tokens"], rows["opus5.0"]["cached_pct"]) == (1_000_000, 75.0)
    assert (rows["gpt6-astra"]["prompt_tokens"], rows["gpt6-astra"]["cached_pct"]) == (
        4_000_000,
        87.5,
    )
    assert rep["totals"]["cache_hit_rate"] == pytest.approx(4_250_000 / 5_000_000)
    assert sum(r["share"] for r in rep["rows"]) == pytest.approx(1.0)


def test_usage_endpoint_defaults_to_all_recorded_days(client):
    _row(at="2026-07-01 10:00:00", inp=10)
    data = client.get("/api/costs/usage").json()
    assert data["range"]["from"] == "2026-07-01"
    assert data["range"]["to"] == datetime.now(timezone.utc).date().isoformat()


@pytest.mark.parametrize(
    "query",
    [
        "from=2026-09-10&to=2026-09-09",
        "from=yesterday",
        "granularity=hour",
        "split=user",
    ],
)
def test_usage_endpoint_rejects_a_bad_request(client, query):
    assert client.get(f"/api/costs/usage?{query}").status_code == 422


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/costs/summary"),
        ("get", "/api/costs/models"),
        ("get", "/api/costs/daily"),
        ("post", "/api/costs/reset-daily"),
    ],
)
def test_the_retired_endpoints_are_gone(client, method, path):
    assert getattr(client, method)(path).status_code in (404, 405)


def test_export_covers_the_selected_range_as_a_download(client):
    _row(inp=100, out=10, at="2026-09-09 12:00:00")
    _row(inp=200, out=20, at="2026-09-10 12:00:00", estimated=("input",))
    r = client.get("/api/costs/export?from=2026-09-10&to=2026-09-10&format=csv")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [row["input_tokens"] for row in rows] == ["200"]
    assert rows[0]["count_source"] == "estimated" and rows[0]["estimated_fields"] == "input"
    assert float(rows[0]["cost_usd"]) == pytest.approx(_cost("opus5.0", inp=200, out=20), abs=1e-6)

    j = client.get("/api/costs/export?from=2026-09-09&to=2026-09-10&format=json")
    assert "attachment" in j.headers["content-disposition"]
    doc = json.loads(j.content)
    assert doc["total_records"] == 2 and doc["range"]["timezone"] == "UTC"
    assert doc["total_cost_usd"] == pytest.approx(sum(r["cost_usd"] for r in doc["records"]))


# ── migration 022 ────────────────────────────────────────────────────────────


def test_migration_022_drops_the_frozen_cost_and_keeps_the_rows(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.execute(
        "CREATE TABLE session_costs (id INTEGER PRIMARY KEY, session_id TEXT, model TEXT, "
        "input_tokens INTEGER, cost_usd REAL NOT NULL DEFAULT 0.0)"
    )
    conn.execute("INSERT INTO session_costs VALUES (1, 's', 'opus5.0', 10, 123.0)")
    conn.commit()
    m022.migrate(conn)
    conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(session_costs)")]
    assert "cost_usd" not in cols
    assert conn.execute("SELECT input_tokens FROM session_costs").fetchall() == [(10,)]
    m022.migrate(conn)  # idempotent
