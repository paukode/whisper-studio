"""FastAPI HTTP handlers for /api/sessions/*.

Split out of server/infrastructure/sessions.py so that module can stay the
persistence/data-access layer. These handlers register on the shared
``sessions.router`` (imported below) via decorator side-effects; sessions.py
imports this module for those effects. The route-only helpers (content-search
snippets, the open-workspace command map) live here because nothing outside the
HTTP layer uses them.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from server.infrastructure import sessions as _s
from server.infrastructure.sessions import (
    CRON_INBOX_ID,
    PROMPT_ROLES,
    _delete_session_sync,
    _get_conn,
    _lock_for,
    _row_to_dict,
    _row_to_summary,
    _safe_col,
    _upsert_session,
    router,
)


@router.get("/api/sessions")
async def list_sessions():
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
    return [_row_to_summary(r) for r in rows]


def _message_text(content) -> str:
    """Text the user actually saw in a chat bubble.

    Plain-string content is returned as is; block-list content contributes
    only its text blocks. Tool payloads (tool_use/tool_result) are skipped
    on purpose: file dumps inside traces would make search match text the
    user never read as a message.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    return ""


_SNIPPET_RADIUS = 40


def _snippet(text: str, start: int, end: int) -> str:
    """A short window of context around a match, flattened to one line."""
    lo = max(0, start - _SNIPPET_RADIUS)
    hi = min(len(text), end + _SNIPPET_RADIUS)
    core = " ".join(text[lo:hi].split())
    return ("…" if lo > 0 else "") + core + ("…" if hi < len(text) else "")


def _search_session_row(row, pattern: re.Pattern) -> str | None:
    """First match snippet for one session, or None.

    Searches user/assistant message text first (what search users usually
    mean by "content"), then transcript segment text. UI-only roles
    (cron_event) never match, mirroring visible_chat_history.
    """
    try:
        history = json.loads(row["chat_history"]) or []
    except (TypeError, ValueError):
        history = []
    for msg in history:
        if not isinstance(msg, dict) or msg.get("role") not in PROMPT_ROLES:
            continue
        text = _message_text(msg.get("content"))
        m = pattern.search(text)
        if m:
            return _snippet(text, m.start(), m.end())
    try:
        segments = json.loads(row["segments"]) or []
    except (TypeError, ValueError):
        segments = []
    for seg in segments:
        text = seg.get("text") if isinstance(seg, dict) else None
        if not isinstance(text, str):
            continue
        m = pattern.search(text)
        if m:
            return _snippet(text, m.start(), m.end())
    return None


# NOTE: declared before GET /api/sessions/{session_id}; FastAPI matches in
# declaration order, so a later literal path would be captured as an id.
@router.get("/api/sessions/search")
async def search_sessions(q: str = "", limit: int = 50):
    """Case-insensitive substring search over session content.

    Backs the sidebar's optional "Search message content" toggle: the
    title-only filter stays client-side, this endpoint reports which
    sessions ALSO match by message/transcript text, newest first, with a
    snippet explaining the hit. Scanning every blob is fine at the current
    scale (tens of sessions, a few hundred KB total); an FTS index is the
    upgrade path if that ever changes.
    """
    needle = q.strip()
    if not needle:
        return {"results": [], "truncated": False}
    limit = max(1, min(limit, 200))
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, chat_history, segments FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
    results = []
    truncated = False
    for i, row in enumerate(rows):
        snippet = _search_session_row(row, pattern)
        if snippet is not None:
            results.append({"id": row["id"], "snippet": snippet})
            if len(results) >= limit:
                # Unscanned rows may hold more matches; tell the client so
                # capped results are never mistaken for a complete answer.
                truncated = i < len(rows) - 1
                break
    return {"results": results, "truncated": truncated}


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "not found"})
    from server.tasks_tracker import get_session_tasks

    data = _row_to_dict(row)
    data["tasks"] = get_session_tasks(session_id)
    return data


@router.put("/api/sessions/{session_id}")
async def save_session(session_id: str, request: Request):
    body = await request.json()
    title = body.get("title", "Untitled Session")
    custom_title = 1 if body.get("customTitle") else 0
    generated_title = 1 if body.get("generatedTitle") else 0
    # The frontend sends `updatedAt` (per the Session type). If absent, fall
    # back to "now" so the row is at least sortable.
    _now = datetime.now(timezone.utc).isoformat()
    created_at = body.get("createdAt") or _now
    updated_at = body.get("updatedAt") or _now
    segments = json.dumps(body.get("segments", []))
    chat_history_frontend = body.get("chatHistory", [])
    speaker_names = json.dumps(body.get("speakerNames", {}))
    workspace_path = body.get("workspacePath", "")
    compaction_count = body.get("compactionCount", 0)
    latched_config = json.dumps(body.get("latchedConfig", {}))

    async with _lock_for(session_id):
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _upsert_session(
                session_id,
                title=title,
                custom_title=custom_title,
                generated_title=generated_title,
                created_at=created_at,
                updated_at=updated_at,
                segments=segments,
                chat_history_frontend=chat_history_frontend,
                speaker_names=speaker_names,
                workspace_path=workspace_path,
                compaction_count=compaction_count,
                latched_config=latched_config,
            ),
        )
    return {"ok": True}


@router.patch("/api/sessions/{session_id}/title")
async def patch_session_title(session_id: str, request: Request):
    body = await request.json()
    title = body.get("title", "")
    custom_title = 1 if body.get("customTitle") else 0
    generated_title = 1 if body.get("generatedTitle") else 0
    with _get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET title=?, custom_title=?, generated_title=? WHERE id=?",
            (title, custom_title, generated_title, session_id),
        )
    return {"ok": True}


@router.patch("/api/sessions/{session_id}/flags")
async def patch_session_flags(session_id: str, request: Request):
    """Update pinned/archived flags. Only fields present in the body change,
    so a pin toggle can't clobber a concurrent archive and vice versa."""
    body = await request.json()
    sets, params = [], []
    for field in ("pinned", "archived"):
        if field in body:
            sets.append(f"{field}=?")
            params.append(1 if body[field] else 0)
    if not sets:
        return JSONResponse({"error": "no flags provided"}, status_code=400)
    params.append(session_id)
    with _get_conn() as conn:
        conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", params)
    return {"ok": True}


# App allowlist for open-workspace. The workspace path comes from the DB,
# never the client; the client only picks one of these names.
_OPEN_WORKSPACE_APPS = ("vscode", "kiro", "finder")


def _open_workspace_cmd(app: str, path: str) -> list[str]:
    import platform

    if platform.system() == "Darwin":
        return {
            "finder": ["open", path],
            "vscode": ["open", "-a", "Visual Studio Code", path],
            "kiro": ["open", "-a", "Kiro", path],
        }[app]
    # Best effort elsewhere: file manager via xdg-open, editors via their CLIs.
    return {
        "finder": ["xdg-open", path],
        "vscode": ["code", path],
        "kiro": ["kiro", path],
    }[app]


@router.post("/api/sessions/{session_id}/open-workspace")
async def open_session_workspace(session_id: str, request: Request):
    """Open the session's workspace folder in an external app."""
    import subprocess

    body = await request.json()
    app = (body.get("app") or "").strip().lower()
    if app not in _OPEN_WORKSPACE_APPS:
        return JSONResponse({"error": f"unknown app: {app or '(none)'}"}, status_code=400)
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = _safe_col(row, "workspace_path", "")
    if not path or not os.path.isdir(path):
        return JSONResponse({"error": "session has no workspace folder"}, status_code=400)
    try:
        result = subprocess.run(
            _open_workspace_cmd(app, path),
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return JSONResponse({"error": f"{app} not available"}, status_code=400)
    except (OSError, subprocess.TimeoutExpired):
        return JSONResponse({"error": f"{app} not available"}, status_code=400)
    return {"ok": True, "path": path}


@router.post("/api/sessions/{session_id}/beacon")
async def beacon_save_session(session_id: str, request: Request):
    """Save via navigator.sendBeacon (POST with JSON body)."""
    body = await request.json()
    body["id"] = session_id
    title = body.get("title", "Untitled Session")
    custom_title = 1 if body.get("customTitle") else 0
    generated_title = 1 if body.get("generatedTitle") else 0
    # See PUT /api/sessions/{id} above for the timestamp fallback.
    _now = datetime.now(timezone.utc).isoformat()
    created_at = body.get("createdAt") or _now
    updated_at = body.get("updatedAt") or _now
    segments = json.dumps(body.get("segments", []))
    chat_history_frontend = body.get("chatHistory", [])
    speaker_names = json.dumps(body.get("speakerNames", {}))
    workspace_path = body.get("workspacePath", "")
    compaction_count = body.get("compactionCount", 0)
    latched_config = json.dumps(body.get("latchedConfig", {}))

    async with _lock_for(session_id):
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _upsert_session(
                session_id,
                title=title,
                custom_title=custom_title,
                generated_title=generated_title,
                created_at=created_at,
                updated_at=updated_at,
                segments=segments,
                chat_history_frontend=chat_history_frontend,
                speaker_names=speaker_names,
                workspace_path=workspace_path,
                compaction_count=compaction_count,
                latched_config=latched_config,
            ),
        )
    return {"ok": True}


@router.post("/api/sessions/{session_id}/branch")
async def branch_session(session_id: str):
    """Fork the current session into a new session with the same history."""
    import uuid
    from datetime import datetime, timezone

    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "not found"})
        base_title = row["title"]
        # Pick a unique branch name
        existing = conn.execute(
            "SELECT title FROM sessions WHERE title LIKE ?", (base_title + " (branch%",)
        ).fetchall()
        branch_num = len(existing) + 1
        new_title = base_title + (" (branch)" if branch_num == 1 else f" (branch {branch_num})")
        new_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO sessions (id, title, custom_title, generated_title, created_at, updated_at, segments, chat_history, speaker_names)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                new_id,
                new_title,
                0,
                0,
                now,
                now,
                row["segments"],
                row["chat_history"],
                row["speaker_names"],
            ),
        )
    return {"new_session_id": new_id, "name": new_title}


@router.get("/api/sessions/{session_id}/context")
async def session_context(session_id: str):
    """Return character/message breakdown for the session."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "not found"})
    history = json.loads(row["chat_history"])
    user_msgs = [m for m in history if m.get("role") == "user"]
    asst_msgs = [m for m in history if m.get("role") == "assistant"]
    tool_results = 0
    tool_uses = 0
    user_chars = 0
    asst_chars = 0
    tool_result_chars = 0
    for m in history:
        content = m.get("content", "")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "tool_result":
                        tool_results += 1
                        tool_result_chars += len(str(b.get("content", "")))
                    elif b.get("type") == "tool_use":
                        tool_uses += 1
                    text = b.get("text", "") or str(b.get("content", ""))
                    if m.get("role") == "assistant":
                        asst_chars += len(text)
                    else:
                        user_chars += len(text)
        else:
            if m.get("role") == "user":
                user_chars += len(str(content))
            else:
                asst_chars += len(str(content))
    total = user_chars + asst_chars + tool_result_chars
    return {
        "total_chars": total,
        "messages": len(history),
        "breakdown": {
            "user_messages": {"count": len(user_msgs), "chars": user_chars},
            "assistant_messages": {"count": len(asst_msgs), "chars": asst_chars},
            "tool_uses": tool_uses,
            "tool_results": {"count": tool_results, "chars": tool_result_chars},
        },
    }


@router.get("/api/sessions/{session_id}/events")
async def session_events(session_id: str, request: Request):
    """Long-lived SSE channel for out-of-band notifications scoped to a
    chat session.

    Subscribes to the agent event_bus for the entire lifetime of the
    connection — unlike the ``/api/chat`` drainer, which only listens
    during an in-flight tool batch. Background cron firings publish to
    the same bus, so this is the path that lets a card appear in the
    chat *without* the user submitting a new turn or refreshing the
    page.

    Only routes events that are explicitly background-flavoured (right
    now: ``type == 'cron_event'``). Agent runtime progress events
    travel through the chat SSE while a turn is in flight and are
    skipped here to avoid double-rendering.

    Sends a comment heartbeat every 15s so proxies don't time out.
    """
    from server.agents.event_bus import event_bus
    from server.utils import ndjson_dumps

    queue = event_bus.subscribe(session_id)

    async def stream():
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # SSE comment — keeps the connection alive without
                    # firing a real event on the frontend.
                    yield ": heartbeat\n\n"
                    continue
                if ev.get("type") == "cron_event":
                    payload = ev.get("cronEvent") or {}
                    yield f"data: {ndjson_dumps({'cron_event': payload})}\n\n"
                elif ev.get("type") == "memory_event":
                    # Memory recall fires pre-stream and extraction fires
                    # after the chat SSE closed, so this long-lived channel
                    # is the only path that can surface them.
                    payload = ev.get("memoryEvent") or {}
                    yield f"data: {ndjson_dumps({'memory_event': payload})}\n\n"
                elif ev.get("type") == "task_event":
                    # Background-task lifecycle (shell/agent/workflow rows in
                    # the unified registry) — delivered here so completion
                    # cards land without an in-flight chat turn.
                    payload = ev.get("taskEvent") or {}
                    yield f"data: {ndjson_dumps({'task_event': payload})}\n\n"
                elif ev.get("type") == "cron_progress":
                    # Live cron-run turn/tool frames. Forwarded in the
                    # team_progress envelope so the existing TeamReportCard
                    # fold renders a running cron job with zero card changes.
                    payload = ev.get("event") or {}
                    yield f"data: {ndjson_dumps({'team_progress': payload})}\n\n"
                elif ev.get("type") == "ci_progress":
                    # Live CI-watch ticks (WS-J), delivered detached so the CI
                    # card updates without an in-flight turn.
                    yield f"data: {ndjson_dumps({'ci_progress': ev})}\n\n"
                elif ev.get("type") == "ci_result":
                    # Terminal CI-watch outcome — a LIVE flip for the rich card.
                    # Durability is separate: _finish also emits a task_event that
                    # persists a chat row, so the outcome survives with no client.
                    payload = ev.get("ciResult") or {}
                    yield f"data: {ndjson_dumps({'ci_result': payload})}\n\n"
                # else: agent progress — handled by the chat SSE drainer,
                # skip here so we don't double-render team_progress rows.
        finally:
            event_bus.unsubscribe(session_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    # The Scheduled Reports inbox is a fixture, not a user session — deleting
    # it would strand orphaned cron runs. Refuse.
    if session_id == CRON_INBOX_ID:
        return JSONResponse(
            {"error": "The Scheduled Reports inbox can't be deleted."},
            status_code=400,
        )
    # Hold the per-session lock so the cascade can't interleave with an
    # in-flight save/append/beacon (all of which take the same lock).
    async with _lock_for(session_id):
        await asyncio.get_event_loop().run_in_executor(None, _delete_session_sync, session_id)
    # Drop the now-unused lock so the map doesn't grow without bound.
    _s._session_locks.pop(session_id, None)
    return {"ok": True}


@router.post("/api/sessions/bulk-delete")
async def bulk_delete_sessions(request: Request):
    """Delete many sessions in one call (sidebar multi-select).

    Runs the exact same per-session locked cascade as the single-delete
    endpoint, sequentially — bulk delete is rare and correctness beats
    parallel speed here. The 500-id cap is a sanity bound, far above any
    real session list.
    """
    body = await request.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return JSONResponse({"error": "ids must be a list of strings"}, status_code=400)
    if len(ids) > 500:
        return JSONResponse({"error": "too many ids (max 500)"}, status_code=400)
    deleted = 0
    for session_id in dict.fromkeys(ids):  # de-dup, preserve order
        if session_id == CRON_INBOX_ID:
            continue  # the inbox fixture is never bulk-deletable
        async with _lock_for(session_id):
            await asyncio.get_event_loop().run_in_executor(None, _delete_session_sync, session_id)
        _s._session_locks.pop(session_id, None)
        deleted += 1
    return {"ok": True, "deleted": deleted}
