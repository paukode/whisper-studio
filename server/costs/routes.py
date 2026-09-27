"""HTTP surface of the cost log (/api/costs).

  GET /api/costs/usage       ranged report for the Costs tab (UTC days)
  GET /api/costs/export      the selected range as CSV or JSON, priced now
  GET /api/costs/session/ID  one session's running totals (composer readout)

Every read is a blocking SQLite query, run off the event loop; an unreadable
cost log answers 503 with the reason.
"""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from server.costs import tracker, usage

router = APIRouter(prefix="/api/costs", tags=["costs"])


async def _read(fn, *args):
    """Run one blocking cost-log read off the event loop. An unreadable log
    is a 503 whose detail says why (the Costs tab shows it), never an empty
    report or a $0 readout."""
    try:
        return await asyncio.to_thread(fn, *args)
    except tracker.CostLogUnreadable as e:
        raise HTTPException(status_code=503, detail=str(e)) from None


async def _range(from_day: str | None, to_day: str | None):
    try:
        return await _read(usage.resolve_range, from_day, to_day)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


@router.get("/usage")
async def api_usage(
    from_day: str | None = Query(None, alias="from"),
    to_day: str | None = Query(None, alias="to"),
    granularity: Literal["day", "week", "month"] = "day",
    split: Literal["model", "session", "source"] = "model",
):
    """Totals, zero-filled buckets and a breakdown for an inclusive UTC-day
    range. ``from`` omitted means from the first recorded day (All time);
    ``to`` omitted means today (UTC)."""
    start, end = await _range(from_day, to_day)
    return await _read(usage.usage_report, start, end, granularity, split)


@router.get("/export")
async def api_export(
    from_day: str | None = Query(None, alias="from"),
    to_day: str | None = Query(None, alias="to"),
    format: Literal["csv", "json"] = "json",
):
    """The range's calls with their count source and a cost priced now, as a
    file download (Content-Disposition attachment, which the Mac shell turns
    into a WKDownload)."""
    start, end = await _range(from_day, to_day)
    name = f"whisper_costs_{start.isoformat()}_{end.isoformat()}.{format}"
    headers = {"Content-Disposition": f"attachment; filename={name}"}
    if format == "csv":
        body = await _read(usage.export_csv, start, end)
        return Response(content=body, media_type="text/csv", headers=headers)
    doc = await _read(usage.export_document, start, end)
    return Response(content=json.dumps(doc), media_type="application/json", headers=headers)


@router.get("/session/{session_id}")
async def api_session_usage(session_id: str):
    """One session's running token/cost totals, for the composer readout.

    The live numbers arrive on the chat stream's usage frames; this rehydrates
    them when a session is reopened (or the app reloaded) so the readout keeps
    counting the whole session instead of restarting at the next turn. It
    also says how many rounds ran on GPT (with the dated list-rate note) or
    had estimated counts, so the readout marks its figure.
    """
    return await _read(usage.session_readout, session_id)
