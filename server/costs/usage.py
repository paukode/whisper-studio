"""The cost log read back over a date range, priced when read.

Days are UTC days, both range ends inclusive, so a day here is the same day
AWS Cost Explorer reports. Weeks start on Monday; a first or last week or
month the range clips is flagged ``partial``. Buckets are zero-filled over the
whole range. Every figure is the stored token counts priced with the current
rate table (tracker.estimate_cost), so a rate correction re-rates history the
same way here, in the export, in the composer readout and in the budget caps.
A call whose prompt is above its model's long-context threshold prices at the
long-context rates, so every sum here holds calls of one tier.

Prompt tokens are normalized across providers (uncached input plus cache
read plus cache write), so GPT and Claude add up.

All functions here are blocking SQLite reads: the routes run them off the
event loop. A read that fails raises tracker.CostLogUnreadable, never an
empty report or $0.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from server.costs import tracker
from server.costs.sources import source_label
from server.costs.tracker import CostLogUnreadable

log = logging.getLogger("whisper-studio")

GRANULARITIES = ("day", "week", "month")
SPLITS = ("model", "session", "source")

# User decision 2026-09-23: GPT on Bedrock stays priced at list rates, and
# every GPT figure says so, dated, so it never pretends to be live.
GPT_BILLED_AS_OF = "2026-09-23"
GPT_NOTE = (
    "Estimate at list rates. AWS billed GPT on Bedrock at $0 for this account "
    f"as of {GPT_BILLED_AS_OF}."
)
GPT_MIXED_NOTE = (
    "Includes GPT on Bedrock, estimated at list rates. AWS billed GPT on Bedrock "
    f"at $0 for this account as of {GPT_BILLED_AS_OF}."
)
UNPRICED_NOTE = "No rate for this model in the pricing table, so its spend shows as $0."
UNPRICED_MIXED_NOTE = "Includes a model with no rate in the pricing table, shown as $0."
LOCAL_NOTE = "On-device model: no charge."

_KEY_COLUMN = {"model": "model", "session": "session_id", "source": "source"}


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _bound(d: date) -> str:
    """A UTC day as the session_costs created_at text form, at 00:00:00."""
    return f"{d.isoformat()} 00:00:00"


def _parse_day(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a YYYY-MM-DD date, got {value!r}") from None


def _first_recorded_day() -> date | None:
    try:
        with tracker._get_conn() as conn:
            row = conn.execute("SELECT MIN(created_at) AS first FROM session_costs").fetchone()
    except Exception as e:  # noqa: BLE001 - never read as empty
        log.error("Cost first-day query failed: %s", e)
        raise CostLogUnreadable("the first recorded day", e) from e
    first = row["first"] if row else None
    return date.fromisoformat(first[:10]) if first else None


def resolve_range(from_day: str | None, to_day: str | None) -> tuple[date, date]:
    """The inclusive UTC-day range a request names. ``to`` defaults to today
    (UTC); ``from`` to the first recorded day, which is how "All time" asks.
    Raises ValueError for a malformed date or a reversed range."""
    end = _parse_day(to_day, "to") if to_day else _utc_today()
    if from_day:
        start = _parse_day(from_day, "from")
    else:
        start = min(_first_recorded_day() or end, end)
    if start > end:
        raise ValueError(f"from ({start}) is after to ({end})")
    return start, end


# ── aggregation ──────────────────────────────────────────────────────────────


@dataclass
class _Tokens:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    calls: int = 0
    estimated_calls: int = 0

    def add(self, row) -> None:
        self.input += row["input_tokens"] or 0
        self.output += row["output_tokens"] or 0
        self.cache_read += row["cache_read"] or 0
        self.cache_write += row["cache_write"] or 0
        self.calls += row["calls"] or 0
        self.estimated_calls += row["estimated_calls"] or 0


def _is_gpt(model: str) -> bool:
    """GPT on Bedrock: routed through the OpenAI provider, or (for a key the
    catalog no longer carries) priced under the OpenAI cache convention."""
    if tracker.cached_in_input_for(model):
        return True
    try:
        from server.openai_bedrock.runtime import is_openai_model

        return is_openai_model(model)
    except Exception:  # noqa: BLE001 - a config read failure is not a GPT row
        return False


def _is_unpriced(model: str) -> bool:
    return tracker.get_model_pricing(model) is None and not tracker.is_expected_free(model)


class _Group:
    """Token sums for one bucket, split key or the whole range, per model and
    long-context tier: each sum holds calls of one tier, so it prices
    linearly."""

    def __init__(self) -> None:
        self.by_tier: dict[tuple[str, bool], _Tokens] = {}

    def add(self, row) -> None:
        self.by_tier.setdefault((row["model"], bool(row["long_ctx"])), _Tokens()).add(row)

    def models(self) -> list[str]:
        return list(dict.fromkeys(m for m, _long in self.by_tier))

    def cost(self) -> float:
        return sum(
            tracker.estimate_cost(
                m, t.input, t.output, t.cache_read, t.cache_write, long_context=long
            )
            for (m, long), t in self.by_tier.items()
        )

    def prompt_tokens(self) -> int:
        return sum(
            tracker.prompt_tokens_for(m, t.input, t.cache_read, t.cache_write)
            for (m, _long), t in self.by_tier.items()
        )

    def total(self, attr: str) -> int:
        return sum(getattr(t, attr) for t in self.by_tier.values())

    def unpriced_calls(self) -> int:
        """Calls of a billed model with no rate in the table: shown at $0,
        which is not what they cost."""
        return sum(t.calls for (m, _long), t in self.by_tier.items() if _is_unpriced(m))

    def cache_savings(self) -> float:
        """What caching saved at list rates, net of the write premium:
        reads cost cache_read instead of input, writes cost cache_write
        instead of input."""
        saved = 0.0
        for (model, long), t in self.by_tier.items():
            rates = tracker.rates_for(model, long)
            if not rates:
                continue
            saved += t.cache_read * (rates["input"] - rates["cache_read"])
            saved -= t.cache_write * (rates["cache_write"] - rates["input"])
        return saved / 1_000_000

    def summary(self) -> dict:
        return {
            "cost_usd": round(self.cost(), 6),
            "prompt_tokens": self.prompt_tokens(),
            "output_tokens": self.total("output"),
            "calls": self.total("calls"),
        }


def _query(start: date, end: date, split: str) -> list:
    key = _KEY_COLUMN[split]
    long_ctx, long_params = tracker.long_context_sql()
    try:
        with tracker._get_conn() as conn:
            return conn.execute(
                f"""
                SELECT substr(created_at, 1, 10) AS day, model, {key} AS k,
                    {long_ctx} AS long_ctx,
                    COUNT(*) AS calls,
                    SUM(CASE WHEN count_source = 'estimated' THEN 1 ELSE 0 END)
                        AS estimated_calls,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
                    COALESCE(SUM(cache_creation_tokens), 0) AS cache_write
                FROM session_costs
                WHERE created_at >= ? AND created_at < ?
                GROUP BY day, model, k, long_ctx
                """,
                (*long_params, _bound(start), _bound(end + timedelta(days=1))),
            ).fetchall()
    except Exception as e:  # noqa: BLE001 - never read as empty
        log.error("Cost usage query failed: %s", e)
        raise CostLogUnreadable("the usage for this range", e) from e


# ── buckets ──────────────────────────────────────────────────────────────────


def _natural_bucket(d: date, granularity: str) -> tuple[date, date]:
    if granularity == "week":
        start = d - timedelta(days=d.weekday())
        return start, start + timedelta(days=6)
    if granularity == "month":
        start = d.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        return start, nxt - timedelta(days=1)
    return d, d


def _buckets(start: date, end: date, granularity: str) -> list[tuple[date, date, bool]]:
    out = []
    cur = start
    while cur <= end:
        nat_start, nat_end = _natural_bucket(cur, granularity)
        b_start, b_end = max(nat_start, start), min(nat_end, end)
        out.append((b_start, b_end, b_start != nat_start or b_end != nat_end))
        cur = nat_end + timedelta(days=1)
    return out


# ── labels ───────────────────────────────────────────────────────────────────


def _model_labels() -> dict[str, str]:
    try:
        from server.infrastructure.config import load_config

        meta = load_config().get("chat_model_meta") or {}
    except Exception:  # noqa: BLE001 - labels are cosmetic, keys still show
        return {}
    return {k: str(v.get("label") or k) for k, v in meta.items() if isinstance(v, dict)}


def _session_titles(ids: list[str]) -> dict[str, str]:
    titles: dict[str, str] = {}
    wanted = [i for i in ids if i]
    try:
        with tracker._get_conn() as conn:
            for i in range(0, len(wanted), 500):
                chunk = wanted[i : i + 500]
                marks = ",".join("?" for _ in chunk)
                for r in conn.execute(
                    f"SELECT id, title FROM sessions WHERE id IN ({marks})", chunk
                ).fetchall():
                    titles[r["id"]] = r["title"] or "Untitled Session"
    except Exception:  # noqa: BLE001 - no sessions table: every id reads as deleted
        pass
    return titles


def _session_label(session_id: str, titles: dict[str, str]) -> tuple[str, bool]:
    """(label, deleted). Cost rows outlive their session (decision 5), so an
    id with no session row is a deleted session, except the ids the app uses
    for work that never had one."""
    if not session_id:
        return "No session", False
    if session_id in titles:
        return titles[session_id], False
    if session_id == "dream":
        return "Memory consolidation", False
    if session_id.startswith("voice-"):
        return "Voice run", False
    return "Deleted session", True


def _note(split: str, models: list[str]) -> str:
    gpt = any(_is_gpt(m) for m in models)
    unpriced = any(_is_unpriced(m) for m in models)
    if split == "model":
        (model,) = models
        if gpt:
            return GPT_NOTE
        if unpriced:
            return UNPRICED_NOTE
        return LOCAL_NOTE if model.startswith("local_") else ""
    parts = [GPT_MIXED_NOTE] if gpt else []
    if unpriced:
        parts.append(UNPRICED_MIXED_NOTE)
    return " ".join(parts)


# ── the report ───────────────────────────────────────────────────────────────


def usage_report(start: date, end: date, granularity: str, split: str) -> dict:
    """GET /api/costs/usage: totals, zero-filled buckets and one row per split
    key for the inclusive UTC-day range."""
    if granularity not in GRANULARITIES or split not in SPLITS:
        raise ValueError(f"bad granularity {granularity!r} or split {split!r}")
    spans = _buckets(start, end, granularity)
    index = {_natural_bucket(s, granularity)[0]: i for i, (s, _e, _p) in enumerate(spans)}
    bucket_groups: list[dict[str, _Group]] = [{} for _ in spans]
    key_groups: dict[str, _Group] = {}
    totals = _Group()

    for row in _query(start, end, split):
        day = date.fromisoformat(row["day"])
        key = row["k"] or ""
        i = index.get(_natural_bucket(day, granularity)[0])
        if i is None:  # outside the range (cannot happen with the WHERE bounds)
            continue
        bucket_groups[i].setdefault(key, _Group()).add(row)
        key_groups.setdefault(key, _Group()).add(row)
        totals.add(row)

    total_cost = totals.cost()
    prompt_total = totals.prompt_tokens()
    cache_read_total = totals.total("cache_read")

    buckets = []
    for (b_start, b_end, partial), groups in zip(spans, bucket_groups, strict=True):
        by_key = {k: g.summary() for k, g in groups.items()}
        buckets.append(
            {
                "start": b_start.isoformat(),
                "end": b_end.isoformat(),
                "partial": partial,
                "cost_usd": round(sum(g.cost() for g in groups.values()), 6),
                "by_key": by_key,
            }
        )

    labels = _model_labels() if split == "model" else {}
    titles = _session_titles(list(key_groups)) if split == "session" else {}
    rows = []
    for key, g in key_groups.items():
        cost = g.cost()
        prompt = g.prompt_tokens()
        deleted = False
        if split == "model":
            label = labels.get(key, key)
        elif split == "session":
            label, deleted = _session_label(key, titles)
        else:
            label = source_label(key)
        rows.append(
            {
                "key": key,
                "label": label,
                "cost_usd": round(cost, 6),
                "share": round(cost / total_cost, 6) if total_cost else 0.0,
                "prompt_tokens": prompt,
                # A percent (0 to 100), as its name says; share and the
                # totals' cache_hit_rate are fractions (0 to 1).
                "cached_pct": round(100 * g.total("cache_read") / prompt, 2) if prompt else 0.0,
                "output_tokens": g.total("output"),
                "calls": g.total("calls"),
                "estimated_calls": g.total("estimated_calls"),
                "note": _note(split, g.models()),
                "deleted": deleted,
            }
        )
    rows.sort(key=lambda r: (-r["cost_usd"], -r["calls"], r["key"]))

    return {
        "range": {"from": start.isoformat(), "to": end.isoformat(), "timezone": "UTC"},
        "granularity": granularity,
        "split": split,
        "totals": {
            "cost_usd": round(total_cost, 6),
            "prompt_tokens": prompt_total,
            "cache_read_tokens": cache_read_total,
            "cache_write_tokens": totals.total("cache_write"),
            "output_tokens": totals.total("output"),
            "calls": totals.total("calls"),
            "estimated_calls": totals.total("estimated_calls"),
            "unpriced_calls": totals.unpriced_calls(),
            "cache_hit_rate": round(cache_read_total / prompt_total, 4) if prompt_total else 0.0,
            "cache_savings_usd": round(totals.cache_savings(), 6),
        },
        "buckets": buckets,
        "rows": rows,
    }


# ── the composer readout ─────────────────────────────────────────────────────


def session_readout(session_id: str) -> dict:
    """GET /api/costs/session/{id}: the session's running totals
    (tracker.get_session_usage), plus how many of its rounds ran on GPT or
    had estimated token counts and the dated note a GPT figure carries
    (decision 2), so the composer marks its figure the way the Costs tab
    marks the session's row."""
    out = tracker.get_session_usage(session_id)
    try:
        with tracker._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT model, COUNT(*) AS rounds,
                    SUM(CASE WHEN count_source = 'estimated' THEN 1 ELSE 0 END) AS estimated
                FROM session_costs WHERE session_id = ? GROUP BY model
                """,
                (session_id,),
            ).fetchall()
    except Exception as e:  # noqa: BLE001 - never read as unmarked
        log.error("Cost session readout query failed: %s", e)
        raise CostLogUnreadable("this session's rounds", e) from e
    gpt = sum(r["rounds"] or 0 for r in rows if _is_gpt(r["model"]))
    out["gpt_rounds"] = gpt
    out["estimated_rounds"] = sum(r["estimated"] or 0 for r in rows)
    if not gpt:
        out["note"] = ""
    else:
        out["note"] = GPT_NOTE if gpt == out["rounds"] else GPT_MIXED_NOTE
    return out


# ── spend for the budget caps ────────────────────────────────────────────────


def spend_between(start: datetime, end: datetime) -> float:
    """USD spent in [start, end), priced now. Both are UTC datetimes."""
    fmt = "%Y-%m-%d %H:%M:%S"
    long_ctx, long_params = tracker.long_context_sql()
    try:
        with tracker._get_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT model, {long_ctx} AS long_ctx,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
                    COALESCE(SUM(cache_creation_tokens), 0) AS cache_write
                FROM session_costs WHERE created_at >= ? AND created_at < ?
                GROUP BY model, long_ctx
                """,
                (*long_params, start.strftime(fmt), end.strftime(fmt)),
            ).fetchall()
    except Exception as e:  # noqa: BLE001 - never read as $0: the daily cap relies on it
        log.error("Cost spend query failed: %s", e)
        raise CostLogUnreadable("the spend for this period", e) from e
    return sum(
        tracker.estimate_cost(
            r["model"],
            r["input_tokens"],
            r["output_tokens"],
            r["cache_read"],
            r["cache_write"],
            long_context=bool(r["long_ctx"]),
        )
        for r in rows
    )


def today_spend_usd() -> float:
    """Spend so far on the current UTC day, the window the daily cap uses."""
    day = _utc_today()
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return spend_between(start, start + timedelta(days=1))


# ── export ───────────────────────────────────────────────────────────────────

EXPORT_FIELDS = (
    "created_at",
    "session_id",
    "source",
    "model",
    "turn_number",
    "input_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "output_tokens",
    "prompt_tokens",
    "count_source",
    "estimated_fields",
    "count_detail",
    "api_duration_ms",
    "cost_usd",
    "note",
)


def export_records(start: date, end: date) -> list[dict]:
    """Every call in the range, oldest first, with its count source and a
    cost priced at export time."""
    try:
        with tracker._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT created_at, session_id, source, model, turn_number, input_tokens,
                    output_tokens, cache_read_tokens, cache_creation_tokens,
                    api_duration_ms, count_source, estimated_fields, count_detail
                FROM session_costs WHERE created_at >= ? AND created_at < ?
                ORDER BY created_at, id
                """,
                (_bound(start), _bound(end + timedelta(days=1))),
            ).fetchall()
    except Exception as e:  # noqa: BLE001 - never export an empty file for a failed read
        log.error("Cost export query failed: %s", e)
        raise CostLogUnreadable("the calls for this range", e) from e
    out = []
    for r in rows:
        rec = dict(r)
        model = rec["model"]
        rec["prompt_tokens"] = tracker.prompt_tokens_for(
            model, rec["input_tokens"], rec["cache_read_tokens"], rec["cache_creation_tokens"]
        )
        rec["cost_usd"] = round(
            tracker.call_cost(
                model,
                rec["input_tokens"],
                rec["output_tokens"],
                rec["cache_read_tokens"],
                rec["cache_creation_tokens"],
            ),
            6,
        )
        rec["note"] = _note("model", [model])
        out.append({f: rec[f] for f in EXPORT_FIELDS})
    return out


def export_document(start: date, end: date) -> dict:
    records = export_records(start, end)
    return {
        "range": {"from": start.isoformat(), "to": end.isoformat(), "timezone": "UTC"},
        "priced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_cost_usd": round(sum(r["cost_usd"] for r in records), 6),
        "total_records": len(records),
        "records": records,
    }


def export_csv(start: date, end: date) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=EXPORT_FIELDS)
    writer.writeheader()
    writer.writerows(export_records(start, end))
    return out.getvalue()
