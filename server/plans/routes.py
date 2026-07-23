"""HTTP routes for saved plan documents. GET /api/plans/{id} serves the raw
markdown to the dock's plan panel; GET /api/plans lists them for a session."""

from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, Response

from server.plans.store import list_plans, read_plan

router = APIRouter(prefix="/api/plans", tags=["plans"])


@router.get("")
async def list_all(session: str | None = None):
    return {"plans": list_plans(session)}


@router.get("/{plan_id}")
async def get_plan(plan_id: str):
    markdown = read_plan(plan_id)
    if markdown is None:
        return Response(
            content=json.dumps({"error": "plan not found"}),
            status_code=404,
            media_type="application/json",
        )
    return PlainTextResponse(markdown, media_type="text/markdown; charset=utf-8")
