"""Durable per-round request snapshots: "model-visible means logged".

Every model round's assembled request (system prompt, tool schemas, message
history, generation params) is written as one gzip JSON file, so any past
request can be reconstructed exactly, long after the turn ran: which passages
were injected, which tool schemas were advertised, which system prompt variant
was active. Without this the assembled envelope existed only in memory and the
server log's one Turn-setup line; debugging "what did the model actually see
on turn N" meant reproducing the turn.

Layout: data_root()/request_snapshots/<session_id>/<ts>-r<round>.json.gz
Retention: files older than RETENTION_DAYS are pruned opportunistically on
write (the tool-result-cache convention), and a per-session cap bounds
pathological turn counts. Writes are best-effort and never fail the turn.

Read-back:
  GET /api/sessions/{session_id}/requests          -> newest-first listing
  GET /api/sessions/{session_id}/requests/{name}   -> the full snapshot JSON
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

router = APIRouter(tags=["request-snapshots"])

RETENTION_DAYS = 7
MAX_PER_SESSION = 500
# One snapshot's JSON is capped before compression; a 1M-token context can
# serialize to tens of MB and the point is debuggability, not an archive.
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,128}$")
_SNAPSHOT_NAME_RE = re.compile(r"^[0-9TZ.\-:]{1,64}-r\d+\.json\.gz$")


def _root() -> str:
    return os.path.join(data_root(), "request_snapshots")


def _session_dir(session_id: str) -> str | None:
    if not _SESSION_ID_RE.match(session_id or ""):
        return None
    return os.path.join(_root(), session_id)


def _prune(session_dir: str) -> None:
    """Drop expired snapshots (all sessions share the age rule) and enforce the
    per-session count cap. Called on write; failures are logged and ignored."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    try:
        names = sorted(os.listdir(session_dir))
    except OSError:
        return
    paths = [os.path.join(session_dir, n) for n in names]
    for p in paths:
        try:
            if os.path.getmtime(p) < cutoff:
                os.unlink(p)
        except OSError:
            pass
    try:
        remaining = sorted(
            (p for p in paths if os.path.exists(p)), key=lambda p: os.path.getmtime(p)
        )
        for p in remaining[: max(0, len(remaining) - MAX_PER_SESSION)]:
            os.unlink(p)
    except OSError:
        pass


def record_request_snapshot(
    *,
    session_id: str,
    round_num: int,
    adapter,
    tools: list[dict],
    messages: list,
    effort_label: str | None = None,
    is_last_round: bool = False,
) -> None:
    """Write one round's assembled request to disk. Best-effort: every failure
    is swallowed after a log line, because a snapshot must never cost a turn.

    Called synchronously-safe (pure CPU + one file write); the runner schedules
    it on the default executor to keep gzip off the event loop.
    """
    try:
        sdir = _session_dir(session_id)
        if not sdir:
            return
        snapshot = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "round": round_num,
            "is_last_round": is_last_round,
            "provider": getattr(adapter, "provider", "unknown"),
            "model_key": getattr(adapter, "model_key", ""),
            "model_id": getattr(adapter, "model_id", ""),
            "effort_label": effort_label,
            "tool_names": [t.get("name", "?") for t in tools],
            "tools": tools,
            "messages": messages,
        }
        describe = getattr(adapter, "describe_request", None)
        if callable(describe):
            try:
                snapshot["request_env"] = describe()
            except Exception as e:  # noqa: BLE001 - adapter-specific, non-fatal
                snapshot["request_env"] = {"error": str(e)}
        raw = json.dumps(snapshot, ensure_ascii=False, default=str)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            snapshot["messages"] = (
                f"(omitted: serialized request was {len(raw)} bytes, "
                f"over the {MAX_SNAPSHOT_BYTES} byte snapshot cap)"
            )
            raw = json.dumps(snapshot, ensure_ascii=False, default=str)
        os.makedirs(sdir, exist_ok=True)
        ts_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
        path = os.path.join(sdir, f"{ts_name}-r{round_num}.json.gz")
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            f.write(raw)
        os.replace(tmp, path)
        _prune(sdir)
    except Exception as e:  # noqa: BLE001 - never let a snapshot cost a turn
        log.warning("request snapshot failed for %s round %s: %s", session_id, round_num, e)


# ── Read-back API ────────────────────────────────────────────────────────────


@router.get("/api/sessions/{session_id}/requests")
async def list_request_snapshots(session_id: str):
    sdir = _session_dir(session_id)
    if not sdir or not os.path.isdir(sdir):
        return {"snapshots": []}
    entries = []
    for name in sorted(os.listdir(sdir), reverse=True):
        if not name.endswith(".json.gz"):
            continue
        p = os.path.join(sdir, name)
        try:
            entries.append(
                {
                    "name": name,
                    "bytes": os.path.getsize(p),
                    "modified": datetime.fromtimestamp(
                        os.path.getmtime(p), tz=timezone.utc
                    ).isoformat(),
                }
            )
        except OSError:
            continue
    return {"snapshots": entries}


@router.get("/api/sessions/{session_id}/requests/{name}")
async def read_request_snapshot(session_id: str, name: str):
    sdir = _session_dir(session_id)
    if not sdir or not _SNAPSHOT_NAME_RE.match(name or ""):
        return JSONResponse({"error": "not found"}, status_code=404)
    path = os.path.join(sdir, name)
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001 - corrupt file is data, not a crash
        return JSONResponse({"error": f"unreadable snapshot: {e}"}, status_code=500)
