"""Re-price historical session_costs rows with the current pricing table.

Past turns stored cost_usd computed at record time, so a pricing fix (corrected
rates, removed Opus fallback, GPT cached-input double-count) only affects new
turns. This recomputes every stored row against server.costs.tracker so the cost
dashboard totals become correct retroactively.

It also renames the legacy "opus" model key to "opus4.6" (and "opus_subagent" to
"opus4.6_subagent") so grouping stays consistent with the renamed config key.

Idempotent: re-running on already-correct rows yields the same values. Back up
storage/sessions.db first.

Usage:
    python3 scripts/reprice_session_costs.py [--dry-run]
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.costs.tracker import DB_PATH, estimate_cost  # noqa: E402

_SUBAGENT = "_subagent"
_LEGACY_RENAME = {"opus": "opus4.6"}


def _pricing_key(model: str) -> str:
    """Strip the _subagent suffix and apply the legacy opus->opus4.6 rename to
    get the key used for pricing lookup."""
    base = model[: -len(_SUBAGENT)] if model.endswith(_SUBAGENT) else model
    return _LEGACY_RENAME.get(base, base)


def _renamed_model(model: str) -> str:
    """The model string to store, with the legacy opus key renamed in place."""
    if model.endswith(_SUBAGENT):
        base = model[: -len(_SUBAGENT)]
        return _LEGACY_RENAME.get(base, base) + _SUBAGENT
    return _LEGACY_RENAME.get(model, model)


def reprice(dry_run: bool = False) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens, cost_usd FROM session_costs"
    ).fetchall()

    changed = 0
    old_total = new_total = 0.0
    for r in rows:
        key = _pricing_key(r["model"])
        new_model = _renamed_model(r["model"])
        new_cost = estimate_cost(
            key,
            r["input_tokens"],
            r["output_tokens"],
            cache_read_tokens=r["cache_read_tokens"],
            cache_creation_tokens=r["cache_creation_tokens"],
            cached_in_input=key.startswith("gpt"),
        )
        old_total += r["cost_usd"] or 0.0
        new_total += new_cost
        if new_model != r["model"] or abs((r["cost_usd"] or 0.0) - new_cost) > 1e-9:
            changed += 1
            if not dry_run:
                conn.execute(
                    "UPDATE session_costs SET model = ?, cost_usd = ? WHERE id = ?",
                    (new_model, new_cost, r["id"]),
                )

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"rows: {len(rows)}  changed: {changed}")
    print(f"total cost: ${old_total:.4f} -> ${new_total:.4f}")
    if dry_run:
        print("(dry run — no rows written)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    reprice(**vars(ap.parse_args()))
