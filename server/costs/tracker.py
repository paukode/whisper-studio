"""Cost tracking system — persists per-turn token usage and USD costs to SQLite.

Provides:
  - Per-turn recording with model breakdown
  - Session-level aggregation
  - Daily/weekly summaries
  - Dashboard API endpoints
  - Cost export (CSV/JSON)
"""

import csv
import io
import json
import logging
import os
import sqlite3
from contextlib import contextmanager

from fastapi import APIRouter
from fastapi.responses import Response

from server.infrastructure.paths import config_dir, repo_root, storage_root

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/costs", tags=["costs"])

STORAGE_DIR = storage_root()
DB_PATH = os.path.join(STORAGE_DIR, "sessions.db")

# ── Model pricing (USD per 1M tokens) ────────────────────────────────
# Sourced from JSON, not code: ``pricing.example.json`` (committed, at the repo
# root next to config.example.json) holds the authoritative default rates, and
# an optional gitignored ``pricing.json`` overlays them PER KEY — the same
# example→override split as config.example.json → config.json. Adding or
# repricing a model is a JSON edit, no code change. Loaded once at import;
# changes apply on server restart.
#
# Keyed by chat_models KEY (e.g. "opus5.0"), not the Bedrock model id. Every
# rate is explicit and there is NO fallback bucket: a model with no entry bills
# $0 and logs loudly (see get_model_pricing) rather than inheriting another
# model's rate. Entry shape:
#   input / output          — required, USD per 1M tokens
#   cache_read / cache_write — optional (default 0.0); prompt-cache read/write
#   cached_in_input          — optional bool; OpenAI convention where
#                              input_tokens already includes the cached portion
PRICING_EXAMPLE_PATH = os.path.join(repo_root(), "pricing.example.json")
PRICING_PATH = os.path.join(config_dir(), "pricing.json")

# Numeric rate fields kept when normalizing an entry (all default to 0.0 except
# input/output, which are required).
_RATE_FIELDS = ("input", "output", "cache_read", "cache_write")


def _coerce_pricing_entry(val: object) -> dict | None:
    """Validate/normalize one raw pricing entry. Returns a clean dict, or None
    for anything malformed (missing/non-numeric input|output) so a typo in the
    file can't take pricing down — the model just bills $0 and logs."""
    if not isinstance(val, dict) or "input" not in val or "output" not in val:
        return None
    try:
        entry = {f: float(val.get(f, 0.0)) for f in _RATE_FIELDS}
    except (TypeError, ValueError):
        return None
    if val.get("cached_in_input"):
        entry["cached_in_input"] = True
    return entry


def _read_pricing_file(path: str) -> dict:
    """Parse one pricing JSON file into {key: entry}. Missing file → {} (the
    overlay is optional). Keys starting with "_" or "$" are annotations and are
    skipped. Unreadable/malformed file → {} and a loud log (never crashes)."""
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:  # unreadable / invalid JSON
        log.error("Ignoring unreadable pricing file %s: %s", path, e)
        return {}
    out: dict[str, dict] = {}
    for key, val in raw.items() if isinstance(raw, dict) else []:
        if key.startswith(("_", "$")):
            continue
        entry = _coerce_pricing_entry(val)
        if entry is None:
            log.error("Skipping malformed pricing entry %r in %s", key, path)
            continue
        out[key] = entry
    return out


def _load_pricing() -> dict:
    """Default rates from pricing.example.json, overlaid PER KEY by pricing.json
    (user entries win). A missing example file degrades to {} (all models bill
    $0 and log) rather than crashing the cost tracker."""
    table = _read_pricing_file(PRICING_EXAMPLE_PATH)
    table.update(_read_pricing_file(PRICING_PATH))
    return table


_MODEL_PRICING = _load_pricing()


def get_model_pricing(model_key: str) -> dict | None:
    """Exact per-model pricing. Returns None (and logs) for an unknown key —
    there is no fallback to another model's rate."""
    pricing = _MODEL_PRICING.get(model_key)
    if pricing is None:
        log.error(
            "No pricing entry for model %r — billing $0. Add it to _MODEL_PRICING.",
            model_key,
        )
    return pricing


def estimate_cost(
    model_key: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cached_in_input: bool = False,
) -> float:
    """Estimate USD cost for a model call, including prompt-cache reads/writes.

    cached_in_input distinguishes the two provider conventions:
      * Anthropic (False, default): input / cache_read / cache_creation are
        disjoint token buckets, each billed once.
      * OpenAI (True): cache_read_tokens are a SUBSET of input_tokens, so the
        cached portion is billed once at the cache_read rate and the remainder
        at the input rate — never both. OpenAI has no cache-creation charge.
    """
    pricing = get_model_pricing(model_key)
    if pricing is None:
        return 0.0
    billable_input = input_tokens - cache_read_tokens if cached_in_input else input_tokens
    if billable_input < 0:
        billable_input = 0
    return (
        (billable_input / 1_000_000 * pricing["input"])
        + (output_tokens / 1_000_000 * pricing["output"])
        + (cache_read_tokens / 1_000_000 * pricing["cache_read"])
        + (cache_creation_tokens / 1_000_000 * pricing["cache_write"])
    )


# ── Database operations ───────────────────────────────────────────────


@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_turn(
    session_id: str,
    turn_number: int,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    api_duration_ms: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> None:
    """Persist a single turn's cost data."""
    try:
        with _get_conn() as conn:
            conn.execute(
                """
                INSERT INTO session_costs
                    (session_id, turn_number, model, input_tokens, output_tokens,
                     cache_read_tokens, cache_creation_tokens, cost_usd, api_duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    session_id,
                    turn_number,
                    model,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_creation_tokens,
                    cost_usd,
                    api_duration_ms,
                ),
            )
    except Exception as e:
        log.error("Failed to record cost: %s", e)


def get_session_costs(session_id: str) -> list[dict]:
    """Get per-turn cost breakdown for a session."""
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM session_costs WHERE session_id = ? ORDER BY turn_number",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_session_summary(session_id: str) -> dict:
    """Get aggregated cost summary for a session."""
    try:
        with _get_conn() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as turns,
                    COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                    COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) as total_cache_read,
                    COALESCE(SUM(cache_creation_tokens), 0) as total_cache_creation,
                    COALESCE(SUM(cost_usd), 0.0) as total_cost_usd,
                    COALESCE(SUM(api_duration_ms), 0) as total_duration_ms
                FROM session_costs WHERE session_id = ?
            """,
                (session_id,),
            ).fetchone()
        if not row:
            return {}
        return dict(row)
    except Exception:
        return {}


def get_model_breakdown(session_id: str = "") -> list[dict]:
    """Get per-model token usage and cost totals. If session_id is empty, returns global."""
    try:
        with _get_conn() as conn:
            if session_id:
                rows = conn.execute(
                    """
                    SELECT model,
                        COUNT(*) as turns,
                        SUM(input_tokens) as input_tokens,
                        SUM(output_tokens) as output_tokens,
                        SUM(cache_read_tokens) as cache_read_tokens,
                        SUM(cost_usd) as cost_usd
                    FROM session_costs WHERE session_id = ?
                    GROUP BY model ORDER BY cost_usd DESC
                """,
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute("""
                    SELECT model,
                        COUNT(*) as turns,
                        SUM(input_tokens) as input_tokens,
                        SUM(output_tokens) as output_tokens,
                        SUM(cache_read_tokens) as cache_read_tokens,
                        SUM(cost_usd) as cost_usd
                    FROM session_costs
                    GROUP BY model ORDER BY cost_usd DESC
                """).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_daily_costs(days: int = 30) -> list[dict]:
    """Get daily cost totals for the last N days."""
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    DATE(created_at) as date,
                    COUNT(*) as turns,
                    SUM(input_tokens) as input_tokens,
                    SUM(output_tokens) as output_tokens,
                    SUM(cost_usd) as cost_usd
                FROM session_costs
                WHERE created_at >= datetime('now', ?)
                GROUP BY DATE(created_at)
                ORDER BY date DESC
            """,
                (f"-{days} days",),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_cache_stats(session_id: str = "") -> dict:
    """Aggregate prompt-cache telemetry, globally or for one session.

    ``est_savings_usd`` is what the cached reads would have cost at full input
    rate minus what they actually cost at the cache-read rate, summed per
    model (pricing differs); models without pricing contribute tokens but no
    savings estimate. This is the live "is caching actually working" surface
    the cloud-prompt-caching work was missing.
    """
    try:
        with _get_conn() as conn:
            where = "WHERE session_id = ?" if session_id else ""
            params = (session_id,) if session_id else ()
            rows = conn.execute(
                f"""
                SELECT model,
                    COALESCE(SUM(input_tokens), 0) as input_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) as cache_read,
                    COALESCE(SUM(cache_creation_tokens), 0) as cache_write
                FROM session_costs {where} GROUP BY model
                """,
                params,
            ).fetchall()
    except Exception:
        return {"read_tokens": 0, "write_tokens": 0, "hit_rate": 0.0, "est_savings_usd": 0.0}

    read_total = write_total = input_total = 0
    savings = 0.0
    for r in rows:
        pricing = _resolve_pricing(r["model"])
        inp = r["input_tokens"] or 0
        rd = r["cache_read"] or 0
        # OpenAI rows record input INCLUSIVE of cached tokens (the
        # cached_in_input convention); normalize so the denominator is
        # uniformly "total prompt tokens" and GPT rows don't deflate the
        # hit rate by double-counting their cached portion.
        if pricing and pricing.get("cached_in_input"):
            inp = max(inp - rd, 0)
        read_total += rd
        write_total += r["cache_write"] or 0
        input_total += inp
        if pricing and rd:
            savings += (rd / 1_000_000) * (pricing["input"] - pricing["cache_read"])
    denominator = read_total + write_total + input_total
    hit_rate = (read_total / denominator) if denominator else 0.0
    return {
        "read_tokens": read_total,
        "write_tokens": write_total,
        "hit_rate": round(hit_rate, 4),
        "est_savings_usd": round(savings, 4),
    }


def _resolve_pricing(model: str) -> dict | None:
    """Pricing rows are keyed by model key; recorded models may carry
    suffixes (e.g. "opus4.8_subagent") — fall back to the LONGEST prefix
    match so "sonnet5_subagent" resolves to sonnet5, never sonnet."""
    if model in _MODEL_PRICING:
        return _MODEL_PRICING[model]
    if model:
        matches = [k for k in _MODEL_PRICING if model.startswith(k)]
        if matches:
            return _MODEL_PRICING[max(matches, key=len)]
    return None


def get_today_total_cost() -> float:
    """Get total cost for today (UTC)."""
    try:
        with _get_conn() as conn:
            row = conn.execute("""
                SELECT COALESCE(SUM(cost_usd), 0.0) as total
                FROM session_costs
                WHERE DATE(created_at) = DATE('now')
            """).fetchone()
        return float(row["total"]) if row else 0.0
    except Exception:
        return 0.0


def get_all_costs_for_export() -> list[dict]:
    """Get all cost records for export."""
    try:
        with _get_conn() as conn:
            rows = conn.execute("""
                SELECT session_id, turn_number, model, input_tokens, output_tokens,
                       cache_read_tokens, cache_creation_tokens, cost_usd,
                       api_duration_ms, created_at
                FROM session_costs ORDER BY created_at
            """).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── API Endpoints ─────────────────────────────────────────────────────


@router.get("/summary")
async def api_cost_summary(session_id: str = ""):
    """Global cost summary across all sessions.

    With ``session_id``: adds that session's live context usage
    (context_used/context_max, from the loop's real per-round token counts)
    so the Stats panel's context bar reflects the active conversation.
    """
    daily = get_daily_costs(30)
    models = get_model_breakdown()
    total = sum(d["cost_usd"] for d in daily) if daily else 0.0
    total_turns = sum(d.get("turns", 0) for d in daily) if daily else 0
    today = get_today_total_cost()
    out = {
        "total_cost_usd": round(total, 4),
        "session_cost_usd": round(total, 4),
        "today_total_usd": round(today, 4),
        "total_turns": total_turns,
        "daily": daily,
        "models": models,
        "cache": get_cache_stats(),
        "pricing": {k: v for k, v in _MODEL_PRICING.items()},
    }
    if session_id:
        from server.chat.loop_hints import context_estimate

        used, cap = context_estimate(session_id)
        if used is not None:
            out["context_used"] = used
            out["context_max"] = cap
    return out


@router.get("/models")
async def api_model_costs():
    """Per-model token usage and cost totals."""
    return {
        "models": get_model_breakdown(),
        "pricing": {k: v for k, v in _MODEL_PRICING.items()},
    }


@router.get("/daily")
async def api_daily_costs(days: int = 30):
    """Daily cost totals for the bar chart."""
    return {"days": get_daily_costs(days)}


@router.post("/reset-daily")
async def api_reset_daily():
    """Delete today's cost records (manual reset)."""
    try:
        with _get_conn() as conn:
            conn.execute("DELETE FROM session_costs WHERE DATE(created_at) = DATE('now')")
            conn.commit()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/export")
async def api_export_costs(format: str = "json"):
    """Export all cost data as JSON or CSV."""
    records = get_all_costs_for_export()

    if format == "csv":
        output = io.StringIO()
        if records:
            writer = csv.DictWriter(output, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)
        content = output.getvalue()
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=whisper_costs.csv"},
        )

    return {
        "records": records,
        "total_records": len(records),
        "total_cost_usd": round(sum(r.get("cost_usd", 0) for r in records), 4),
    }
