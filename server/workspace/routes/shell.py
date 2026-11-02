"""Stop what a session's stopped turn started (the Stop and ESC kill switch)."""

import asyncio
import json

from fastapi import Request
from fastapi.responses import Response

from .. import router


@router.post("/shell/tasks/stop")
async def ws_shell_tasks_stop(request: Request):
    """Stop what the session's stopped turn started.

    Wired to the Stop and ESC kill switch (streamControl.killSessionStream).
    ``since`` is when the stopped turn's stream began (epoch seconds): the
    foreground commands and background shell tasks the session started from
    then on die with the turn, while work from earlier turns (the dev server
    the app is running on) keeps going. The kills run off the event loop so a
    process that ignores SIGTERM cannot freeze other sessions."""
    from server.tasks.handoff import stop_turn_work

    body = await request.json()
    session_id = (body.get("session_id") or "").strip()
    since = body.get("since")
    if not session_id or isinstance(since, bool) or not isinstance(since, (int, float)):
        return Response(
            content=json.dumps({"error": "session_id and since (epoch seconds) required"}),
            status_code=400,
            media_type="application/json",
        )
    return await asyncio.to_thread(stop_turn_work, session_id, float(since))
