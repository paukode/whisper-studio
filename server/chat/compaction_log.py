"""Durable compaction brackets: every compaction writes a start record before
touching the history and an end record with the outcome after, to a
per-session JSONL file. A crash mid-compaction therefore leaves a detectable
unmatched start instead of a silently half-applied history; boot reconciles
those orphans into explicit "orphaned" end records so the log always tells the
truth about what happened.

Layout: data_root()/compactions/<session_id>.jsonl, one JSON object per line:
  {ts, event: "start", trigger, before_chars}
  {ts, event: "end", outcome, after_chars, detail?}

Outcomes: "pruned-only" (the prune rung freed enough, no summary was paid
for), "session-memory", "summary", "truncation", "unchanged", "error",
"orphaned" (synthesized at boot for a crashed bracket).

Read-back: GET /api/sessions/{session_id}/compactions
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

router = APIRouter(tags=["compaction-log"])

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,128}$")
# Keep the newest N records per session; the log is diagnostics, not history.
MAX_RECORDS = 400


def _path(session_id: str) -> str | None:
    if not _SESSION_ID_RE.match(session_id or ""):
        return None
    root = os.path.join(data_root(), "compactions")
    return os.path.join(root, f"{session_id}.jsonl")


def _append(session_id: str, record: dict) -> None:
    path = _path(session_id)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        _trim(path)
    except Exception as e:  # noqa: BLE001 - the log must never cost a turn
        log.warning("compaction log append failed for %s: %s", session_id, e)


def _trim(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > MAX_RECORDS:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines[-MAX_RECORDS:])
    except OSError:
        pass


def record_start(session_id: str, *, trigger: str, before_chars: int) -> None:
    """The durable lock half of the bracket — written BEFORE any strategy runs,
    so a crash mid-compaction is evidence (an unmatched start), not silence."""
    _append(session_id, {"event": "start", "trigger": trigger, "before_chars": before_chars})


def record_end(session_id: str, *, outcome: str, after_chars: int, detail: str = "") -> None:
    rec = {"event": "end", "outcome": outcome, "after_chars": after_chars}
    if detail:
        rec["detail"] = detail
    _append(session_id, rec)


def reconcile_orphans() -> int:
    """Boot pass: any session log whose LAST record is a start crashed
    mid-compaction. Write a synthetic "orphaned" end so the bracket closes
    honestly, and log a warning naming the session. Returns the orphan count."""
    root = os.path.join(data_root(), "compactions")
    if not os.path.isdir(root):
        return 0
    orphans = 0
    for name in os.listdir(root):
        if not name.endswith(".jsonl"):
            continue
        session_id = name[: -len(".jsonl")]
        path = os.path.join(root, name)
        try:
            with open(path, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
            if not lines:
                continue
            last = json.loads(lines[-1])
        except Exception:  # noqa: BLE001 - unreadable log = skip, not crash
            continue
        if last.get("event") == "start":
            orphans += 1
            log.warning(
                "session %s has an orphaned compaction bracket (crashed mid-compaction "
                "at %s); closing it as 'orphaned'",
                session_id,
                last.get("ts"),
            )
            record_end(session_id, outcome="orphaned", after_chars=-1)
    return orphans


@router.get("/api/sessions/{session_id}/compactions")
async def list_compactions(session_id: str):
    path = _path(session_id)
    if not path or not os.path.isfile(path):
        return {"records": []}
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return {"records": records}
