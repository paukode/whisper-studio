"""The cost log: one SQLite row per billed model call, priced when read.

Provides:
  - the rate table (pricing.example.json overlaid by the user's pricing.json)
  - estimate_cost / prompt_token_total, the one pricing and normalization rule
  - record_turn, the only writer of session_costs
  - get_session_usage, the composer readout and the session budget cap

Rows store token counts and their provenance, never a dollar figure: every
reader prices the stored counts with the current rate table, so a rate
correction re-rates history everywhere at once. The ranged report, the
export and the daily cap live in server.costs.usage; the HTTP routes in
server.costs.routes.
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager

from server.infrastructure.paths import config_dir, repo_root, storage_root

log = logging.getLogger("whisper-studio")

STORAGE_DIR = storage_root()
DB_PATH = os.path.join(STORAGE_DIR, "sessions.db")

# ── Model pricing (USD per 1M tokens) ────────────────────────────────
# Sourced from JSON, not code: ``pricing.example.json`` (committed, at the repo
# root next to config.example.json) holds the authoritative default rates, and
# an optional gitignored ``pricing.json`` of the user's own overrides overlays
# them PER KEY. Adding or repricing a model is a JSON edit, no code change.
# Loaded once at import; changes apply on server restart.
#
# Keyed by chat_models KEY (e.g. "opus5.0"), not the Bedrock model id. Every
# rate is explicit and there is NO fallback bucket: a model with no entry bills
# $0 and logs loudly (see get_model_pricing) rather than inheriting another
# model's rate. Entry shape:
#   input / output          : required, USD per 1M tokens
#   cache_read / cache_write : optional (default 0.0); prompt-cache read/write
#   cached_in_input          : optional bool; OpenAI convention where
#                              input_tokens already includes the cached and
#                              the cache-written portions
#   long_context             : optional {"above": N, input, output, cache_read,
#                              cache_write}; a call whose prompt is above N
#                              tokens bills ALL its tokens at these rates
#                              (the GPT cards' 272K break)
PRICING_EXAMPLE_PATH = os.path.join(repo_root(), "pricing.example.json")
PRICING_PATH = os.path.join(config_dir(), "pricing.json")

# Numeric rate fields kept when normalizing an entry (all default to 0.0 except
# input/output, which are required).
_RATE_FIELDS = ("input", "output", "cache_read", "cache_write")


def _coerce_rates(val: object) -> dict | None:
    if not isinstance(val, dict) or "input" not in val or "output" not in val:
        return None
    try:
        return {f: float(val.get(f, 0.0)) for f in _RATE_FIELDS}
    except (TypeError, ValueError):
        return None


def _coerce_pricing_entry(val: object) -> dict | None:
    """Validate/normalize one raw pricing entry. Returns a clean dict, or None
    for anything malformed (missing/non-numeric input|output, a long_context
    tier without its rates or a positive ``above``) so a typo in the file
    can't take pricing down; the model just bills $0 and logs."""
    entry = _coerce_rates(val)
    if entry is None:
        return None
    if val.get("cached_in_input"):
        entry["cached_in_input"] = True
    if "long_context" in val:
        raw = val["long_context"]
        tier = _coerce_rates(raw)
        try:
            above = int(raw.get("above")) if isinstance(raw, dict) else 0
        except (TypeError, ValueError):
            above = 0
        if tier is None or above <= 0:
            return None
        entry["long_context"] = {"above": above, **tier}
    return entry


def _read_pricing_file(path: str) -> dict:
    """Parse one pricing JSON file into {key: entry}. Missing file gives {} (the
    overlay is optional). Keys starting with "_" or "$" are annotations and are
    skipped. Unreadable/malformed file gives {} and a loud log (never crashes)."""
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
    $0 and log) rather than crashing the cost tracker.

    An override entry equal to a rate some release shipped for that key is a
    stale seeded copy of an old template, not a user decision: it is skipped
    (and logged) so a later rate correction is never shadowed."""
    from server.costs.pricing_history import is_shipped

    table = _read_pricing_file(PRICING_EXAMPLE_PATH)
    for key, entry in _read_pricing_file(PRICING_PATH).items():
        if is_shipped(key, entry):
            if entry != table.get(key):
                log.info(
                    "pricing.json entry for %r equals a rate an earlier release shipped; "
                    "using the current default instead",
                    key,
                )
            continue
        log.info("pricing.json overrides the rates for %r", key)
        table[key] = entry
    return table


_MODEL_PRICING = _load_pricing()

# Keys already reported as unpriced: readers price history on every request,
# so one ERROR line per key and process is loud enough.
_reported_unpriced: set[str] = set()


def is_expected_free(model_key: str) -> bool:
    """On-device models cost $0 by design; so does the placeholder key a
    call with no resolvable model records. Neither is a missing cloud rate."""
    return model_key.startswith("local_") or model_key in ("unknown", "")


def get_model_pricing(model_key: str) -> dict | None:
    """Exact per-model pricing. Returns None (and logs) for an unknown key:
    there is no fallback to another model's rate."""
    pricing = _MODEL_PRICING.get(model_key)
    if pricing is None and model_key not in _reported_unpriced:
        _reported_unpriced.add(model_key)
        log.log(
            logging.DEBUG if is_expected_free(model_key) else logging.ERROR,
            "No pricing entry for model %r: billing $0. Add it to pricing.example.json.",
            model_key,
        )
    return pricing


def rates_for(model_key: str, long_context: bool = False) -> dict | None:
    """The rates one tier of a model bills at: its long-context rates when
    asked for and the entry has them, else its standard rates."""
    pricing = get_model_pricing(model_key)
    if pricing is None:
        return None
    if long_context and pricing.get("long_context"):
        return pricing["long_context"]
    return pricing


def is_long_context(
    model_key: str, input_tokens: int, cache_read_tokens: int = 0, cache_creation_tokens: int = 0
) -> bool:
    """True when ONE call's prompt is above its model's long-context
    threshold, so all of that call's tokens bill at the long-context rates.
    Never ask it about a sum of calls: many short prompts add up to a long
    one."""
    tier = (_MODEL_PRICING.get(model_key) or {}).get("long_context")
    if not tier:
        return False
    return (
        prompt_tokens_for(model_key, input_tokens, cache_read_tokens, cache_creation_tokens)
        > tier["above"]
    )


def call_cost(
    model_key: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """USD cost of ONE model call, at the tier its own prompt falls in. A
    running total is the sum of its calls' call_cost, never the price of the
    summed counts."""
    return estimate_cost(
        model_key,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
        long_context=is_long_context(
            model_key, input_tokens, cache_read_tokens, cache_creation_tokens
        ),
    )


def long_context_sql() -> tuple[str, list]:
    """``(expression, params)``: SQL over one session_costs row, 1 when that
    call's prompt was above its model's long-context threshold (the test
    is_long_context makes), else 0. Readers group by it, so each group sums
    calls of one tier and prices linearly."""
    whens: list[str] = []
    params: list = []
    for key, entry in _MODEL_PRICING.items():
        tier = entry.get("long_context")
        if not tier:
            continue
        prompt = (
            "MAX(input_tokens, cache_read_tokens + cache_creation_tokens)"
            if entry.get("cached_in_input")
            else "(input_tokens + cache_read_tokens + cache_creation_tokens)"
        )
        whens.append(f"WHEN ? THEN ({prompt} > ?)")
        params += [key, tier["above"]]
    if not whens:
        return "0", []
    return f"(CASE model {' '.join(whens)} ELSE 0 END)", params


def estimate_cost(
    model_key: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    *,
    long_context: bool = False,
) -> float:
    """USD cost of these token counts at ONE tier of the model's rates (its
    long-context rates when ``long_context``), including prompt-cache reads
    and writes. Use call_cost for a single call: it picks the tier.

    The provider convention comes from the model's pricing entry
    (``cached_in_input``), never from the caller, so no call site can drop it:
      * Anthropic (flag absent): input / cache_read / cache_creation are
        disjoint token buckets, each billed once.
      * OpenAI (flag set): cache_read_tokens and cache_creation_tokens are
        SUBSETS of input_tokens, so the cached and the written portions are
        billed once, at the cache_read and cache_write rates, and only the
        remainder at the input rate, never both.

    Linear in every count, so pricing the token sums of calls of one tier
    equals summing their call_cost.
    """
    pricing = get_model_pricing(model_key)
    if pricing is None:
        return 0.0
    cached_in_input = bool(pricing.get("cached_in_input"))
    rates = rates_for(model_key, long_context)
    billable_input = (
        input_tokens - cache_read_tokens - cache_creation_tokens
        if cached_in_input
        else input_tokens
    )
    if billable_input < 0:
        billable_input = 0
    return (
        (billable_input / 1_000_000 * rates["input"])
        + (output_tokens / 1_000_000 * rates["output"])
        + (cache_read_tokens / 1_000_000 * rates["cache_read"])
        + (cache_creation_tokens / 1_000_000 * rates["cache_write"])
    )


def prompt_token_total(
    input_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cached_in_input: bool = False,
) -> int:
    """Total prompt tokens for one call, across both provider conventions.

    Providers disagree on what ``input_tokens`` counts, so it is never the
    prompt size on its own:
      * Anthropic (cached_in_input False): input / cache_read / cache_creation
        are disjoint buckets; a fully cached prompt reports a handful of
        input tokens and the rest as cache reads. Sum all three.
      * OpenAI (True): cache_read and cache_creation are SUBSETS of
        input_tokens, which is already the whole prompt. Adding either
        would double it.

    Same flag semantics as estimate_cost, from the same two sources: the
    adapter attribute live, the pricing entry for recorded rows
    (prompt_tokens_for).
    """
    if cached_in_input:
        return max(input_tokens, cache_read_tokens + cache_creation_tokens)
    return input_tokens + cache_read_tokens + cache_creation_tokens


def cached_in_input_for(model_key: str) -> bool:
    """The OpenAI convention flag of a recorded model key, from its rates."""
    return bool((_MODEL_PRICING.get(model_key) or {}).get("cached_in_input"))


def prompt_tokens_for(
    model_key: str, input_tokens: int, cache_read_tokens: int, cache_creation_tokens: int
) -> int:
    """prompt_token_total for recorded rows of one model key."""
    return prompt_token_total(
        input_tokens,
        cache_read_tokens,
        cache_creation_tokens,
        cached_in_input=cached_in_input_for(model_key),
    )


# ── Database operations ───────────────────────────────────────────────


# The current session_costs shape. Migrations 001, 021 and 022 build it on a
# database the app owns; this bootstrap creates it where no migration ran yet
# (tests, a fresh file), the same split as grounding_store._ensure_table.
_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS session_costs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        turn_number INTEGER NOT NULL,
        model TEXT NOT NULL,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        api_duration_ms INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        source TEXT NOT NULL DEFAULT '',
        count_source TEXT NOT NULL DEFAULT 'reported',
        estimated_fields TEXT NOT NULL DEFAULT '',
        count_detail TEXT NOT NULL DEFAULT ''
    )
"""
_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_session_costs_session ON session_costs(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_session_costs_created ON session_costs(created_at)",
)
_ensured: set[str] = set()


@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if DB_PATH not in _ensured:
            conn.execute(_TABLE_DDL)
            for ddl in _INDEX_DDL:
                conn.execute(ddl)
            _ensured.add(DB_PATH)
        yield conn
        conn.commit()
    finally:
        conn.close()


class CostLogUnreadable(RuntimeError):
    """The cost log could not be read. Readers raise it instead of reading
    as empty or $0: a spend that silently reads as nothing would disarm the
    caps and blank the Costs tab with no reason given."""

    def __init__(self, what: str, error: Exception):
        super().__init__(f"Could not read {what} from the cost log: {error}")


# Token fields an adapter can report as estimated (characters / 4 of the
# posted body or of the received content, because the payload carried none).
ESTIMATABLE_FIELDS = ("input", "output", "cache_read", "cache_write")


def record_turn(
    session_id: str,
    turn_number: int,
    model: str,
    input_tokens: int,
    output_tokens: int,
    api_duration_ms: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    *,
    source: str,
    estimated: tuple[str, ...] | list[str] = (),
    detail: dict | None = None,
) -> None:
    """Persist one billed model call: its token counts, never a price.

    ``source`` names the caller (server.costs.sources); an unknown one raises,
    so no spend lands unlabelled. ``estimated`` lists the token fields the
    adapter estimated instead of reading them from the provider's payload
    (the row is then ``count_source`` 'estimated'); ``detail`` holds both
    reported sources when they disagreed (the row carries the authoritative
    one)."""
    from server.costs.sources import require_source

    require_source(source)
    bad = [f for f in estimated if f not in ESTIMATABLE_FIELDS]
    if bad:
        raise ValueError(f"unknown estimated fields {bad!r}")
    fields = ",".join(f for f in ESTIMATABLE_FIELDS if f in estimated)
    try:
        with _get_conn() as conn:
            conn.execute(
                """
                INSERT INTO session_costs
                    (session_id, turn_number, model, input_tokens, output_tokens,
                     cache_read_tokens, cache_creation_tokens, api_duration_ms,
                     source, count_source, estimated_fields, count_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    session_id,
                    turn_number,
                    model,
                    int(input_tokens or 0),
                    int(output_tokens or 0),
                    int(cache_read_tokens or 0),
                    int(cache_creation_tokens or 0),
                    int(api_duration_ms or 0),
                    source,
                    "estimated" if fields else "reported",
                    fields,
                    json.dumps(detail, sort_keys=True) if detail else "",
                ),
            )
    except Exception as e:
        log.error("Failed to record cost: %s", e)


def get_session_costs(session_id: str) -> list[dict]:
    """Every recorded call of one session, oldest first (raw token rows)."""
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM session_costs WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


_EMPTY_USAGE = {"prompt_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "rounds": 0}


def get_session_usage(session_id: str) -> dict:
    """Running token/cost totals for one session, as the live readout shows
    them, priced with the current rate table. Raises CostLogUnreadable when
    the log cannot be read.

    Grouped by model because the cache convention (and so what ``input_tokens``
    means) is per-model: each group is normalized with prompt_token_total before
    summing, so a session that mixed Claude and GPT turns still adds up. And by
    long-context tier, so each group prices at one tier.
    """
    long_ctx, long_params = long_context_sql()
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT model, {long_ctx} AS long_ctx,
                    COUNT(*) as rounds,
                    COALESCE(SUM(input_tokens), 0) as input_tokens,
                    COALESCE(SUM(output_tokens), 0) as output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) as cache_read,
                    COALESCE(SUM(cache_creation_tokens), 0) as cache_write
                FROM session_costs WHERE session_id = ? GROUP BY model, long_ctx
                """,
                (*long_params, session_id),
            ).fetchall()
    except Exception as e:
        log.error("Cost session usage query failed: %s", e)
        raise CostLogUnreadable("this session's spend", e) from e

    out = dict(_EMPTY_USAGE)
    for r in rows:
        model = r["model"]
        inp, rd, wr = r["input_tokens"] or 0, r["cache_read"] or 0, r["cache_write"] or 0
        out["prompt_tokens"] += prompt_tokens_for(model, inp, rd, wr)
        out["output_tokens"] += r["output_tokens"] or 0
        out["cost_usd"] += estimate_cost(
            model, inp, r["output_tokens"] or 0, rd, wr, long_context=bool(r["long_ctx"])
        )
        out["rounds"] += r["rounds"] or 0
    out["cost_usd"] = round(out["cost_usd"], 6)
    return out
