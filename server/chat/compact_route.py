"""POST /api/sessions/{session_id}/compact: the /compact command.

The client sends the session's whole history (it owns the conversation rows)
and an optional note. The server summarizes the prompt history once and
appends a ``compact_summary`` row to the stored session. The row is
backend-owned, like agent reports, so a later frontend save keeps it. From
then on visible_chat_history sends the model that row and what follows it,
never the rows before it. A failed summary appends nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

# The longest note the user can add to /compact.
MAX_NOTE_CHARS = 2000


@router.post("/api/sessions/{session_id}/compact")
async def compact_session(session_id: str, request: Request):
    from server.chat.attachment_context import rebuild_history_message
    from server.chat.compaction import CompactionError, compact_on_request
    from server.infrastructure.sessions import append_message, visible_chat_history

    body = await request.json()
    history = body.get("history")
    model_key = body.get("model")
    note = body.get("note") or ""
    if not isinstance(history, list) or not isinstance(model_key, str) or not model_key:
        raise HTTPException(400, "history (a list) and model are required.")
    if not isinstance(note, str) or len(note) > MAX_NOTE_CHARS:
        raise HTTPException(400, f"The note must be text of at most {MAX_NOTE_CHARS} characters.")
    messages = [rebuild_history_message(m) for m in visible_chat_history(history)]
    if not messages:
        raise HTTPException(400, "There is nothing to compact yet.")
    try:
        summary, before, after = await compact_on_request(messages, model_key, session_id, note)
    except CompactionError as exc:
        raise HTTPException(502, str(exc)) from None
    row = {
        "role": "compact_summary",
        "content": summary,
        "timestamp": datetime.now(UTC).isoformat(),
        "compactSummary": {
            "note": note.strip(),
            "messages": len(messages),
            "before_chars": before,
            "after_chars": after,
        },
    }
    await append_message(session_id, row)
    return row
