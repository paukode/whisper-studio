"""
Cron scheduler for Whisper Studio.

Schedules recurring tasks using APScheduler (optional dependency). Falls back
gracefully if APScheduler is not installed.

A job's ``schedule`` is a discriminated union:

    {"type": "interval", "seconds": 1800}                       # every N (anchored)
    {"type": "cron", "hour": 9, "minute": 0,                    # wall-clock, tz-aware
     "day_of_week": "mon-fri", "tz": "America/New_York"}
    {"type": "at", "run_at": "2026-07-06T09:00:00",             # one-shot, auto-disables
     "tz": "America/New_York"}

Timezone defaults to the host system zone (from /etc/localtime) unless a
``cron_timezone`` config value or a per-job ``schedule.tz`` overrides it, so
"daily at 09:00" fires at the intended local hour and stays correct across DST.

Legacy jobs (``interval_minutes`` only) are migrated in memory on load to
``{"type": "interval", "seconds": interval_minutes*60}`` with no change to
their firing cadence.
"""

import asyncio
import json
import logging
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter

# The Bedrock run loop lives in cron_run.py (kept out of this file for the size
# budget). Re-exported here so the existing names — and the two thread launch
# sites below — read unchanged. cron_run reaches back for load_cron_jobs /
# _push_result / _server_loop / the in-progress state LAZILY, so this
# module-level import is not a cycle.
from server.cron_run import (
    _assemble_cron_tools,  # noqa: F401  (re-exported for callers/tests)
    _execute_cron_prompt,
)
from server.cron_schedule import (
    _parse_dt,
    _zone,
    compute_next_run,
    cron_catch_up_due,
    schedule_label,
)
from server.cron_schedule import (
    build_trigger as _build_trigger,
)

# Schedule-domain logic (timezone, validation, labels, triggers, next-run)
# lives in cron_schedule.py to keep this runtime file under the size budget.
# Aliased so existing call sites read unchanged.
from server.cron_schedule import (
    validate_schedule as _validate_schedule,
)
from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/cron", tags=["cron"])

DATA_DIR = data_root()
CRON_PATH = os.path.join(DATA_DIR, "cron_jobs.json")

_scheduler = None

# Captured at init_scheduler() so background threads can dispatch coroutines
# (append_message) and live-publish events from outside the event loop.
_server_loop: asyncio.AbstractEventLoop | None = None

# Serializes read-modify-write on cron_jobs.json so a concurrent
# toggle+delete+create can't last-write-wins over each other.
_CRON_FILE_LOCK = threading.Lock()

# Guards against a run-now firing on top of a still-running execution (the
# scheduled path is already guarded by APScheduler max_instances=1).
_IN_PROGRESS_LOCK = threading.Lock()
_in_progress: set[str] = set()


def _utc_now_iso() -> str:
    # Trailing Z for parity with how the frontend renders timestamps.
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_past(iso: str | None) -> bool:
    """True when an ISO timestamp is at or before now (UTC)."""
    if not iso:
        return False
    try:
        dt = _parse_dt(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= datetime.now(timezone.utc)
    except Exception:
        return False


def _at_job_already_fired(job: dict) -> bool:
    """A one-shot 'at' job that can no longer arm: it has fired (fired_at or
    last_run set), or its instant has already passed."""
    sch = job.get("schedule") or {}
    if sch.get("type") != "at":
        return False
    if job.get("fired_at") or job.get("last_run"):
        return True
    return _is_past(sch.get("run_at"))


def _next_run_iso(job: dict) -> str | None:
    """Next fire time for a job: null when disabled, else the live scheduler's
    value (falling back to a pure projection)."""
    if not job.get("enabled", True):
        return None
    jid = job.get("id")
    if _scheduler is not None and jid:
        try:
            j = _scheduler.get_job(jid)
            if j is not None and j.next_run_time is not None:
                return j.next_run_time.isoformat()
        except Exception:
            pass
    projected = compute_next_run(job.get("schedule") or {})
    # A projection at or before now never arms (a one-shot 'at' whose instant
    # has passed projects its own past run_at). Report no next run rather than a
    # stale timestamp the UI would count down from.
    if _is_past(projected):
        return None
    return projected


# ── Persistence ──────────────────────────────────────────────────────────────


def _migrate_job(job: dict) -> dict:
    """Backfill a job read from disk to the current shape. Idempotent.

    A legacy interval_minutes-only job becomes an equivalent interval schedule
    with NO change to its cadence (never silently reinterpreted as wall-clock).
    """
    if not isinstance(job.get("schedule"), dict):
        job["schedule"] = {
            "type": "interval",
            "seconds": max(10, int(float(job.get("interval_minutes", 60)) * 60)),
        }
    job.setdefault("orphaned", False)
    job.setdefault("schema_version", 2)
    return job


def load_cron_jobs() -> list[dict]:
    try:
        with open(CRON_PATH) as f:
            jobs = json.load(f).get("jobs", [])
    except Exception:
        return []
    return [_migrate_job(j) for j in jobs]


def save_cron_jobs(jobs: list[dict]):
    """Atomic write (temp file + os.replace) so a crash can't truncate."""
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".cron_jobs.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"jobs": jobs}, f, indent=2)
        os.replace(tmp, CRON_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _mutate_jobs(fn):
    """Locked read-modify-write on cron_jobs.json. ``fn(jobs)`` mutates the list
    in place and returns any result value, which this returns."""
    with _CRON_FILE_LOCK:
        jobs = load_cron_jobs()
        result = fn(jobs)
        save_cron_jobs(jobs)
        return result


# ── Inline-in-chat delivery ──────────────────────────────────────────────────


def _resolve_target_session(session_id: str) -> str:
    """Where a firing's card should land: the owning session, unless it was
    deleted or archived — then the pinned Scheduled Reports inbox."""
    try:
        from server.infrastructure.sessions import ensure_cron_inbox, get_session_meta

        if not session_id:
            return ensure_cron_inbox()
        meta = get_session_meta(session_id)
        if meta is None or meta.get("archived"):
            return ensure_cron_inbox()
        return session_id
    except Exception:
        return session_id


def _emit_cron_event(
    session_id: str,
    *,
    event_type: str,
    cron_id: str,
    cron_name: str,
    schedule_label_str: str | None = None,
    next_run: str | None = None,
    interval_minutes: float | None = None,
    text: str | None = None,
    status: str | None = None,
    run_id: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Persist a cron_event row into chat_history AND publish it live.

    The persisted row matches the frontend ChatMessage shape (``cronEvent``
    nested, empty ``content``, mirrored ``timestamp``); the event-bus event is
    ``{type: "cron_event", cronEvent: {...}}`` so the SSE drainers can
    discriminate it. Safe to call from any thread. No-ops if the loop hasn't
    been captured yet.
    """
    if not session_id:
        return

    timestamp = _utc_now_iso()
    payload: dict = {
        "event_type": event_type,
        "cron_id": cron_id,
        "cron_name": cron_name,
        "timestamp": timestamp,
    }
    if schedule_label_str is not None:
        payload["schedule_label"] = schedule_label_str
    if next_run is not None:
        payload["next_run"] = next_run
    if interval_minutes is not None:
        payload["interval_minutes"] = interval_minutes
    if text is not None:
        payload["text"] = text
    if status is not None:
        payload["status"] = status
    if run_id is not None:
        payload["run_id"] = run_id
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms

    # Delegates to the generalized session-event service (persist + publish);
    # the payload shape above is unchanged so the frontend needs no changes.
    from server.tasks.events import emit_session_event

    emit_session_event(session_id, role="cron_event", payload_key="cronEvent", payload=payload)


def _push_result(
    job: dict,
    text: str,
    status: str = "ok",
    *,
    run_id: str,
    duration_ms: int | None = None,
):
    """Record a finished run to cron_runs AND surface it inline in the owning
    (or inbox) session."""
    job_id = job.get("id", "")
    job_name = job.get("name", "(unknown)")
    session_id = job.get("session_id", "")
    sch = job.get("schedule") or {}
    next_run = _next_run_iso(job)

    target = _resolve_target_session(session_id)

    try:
        from server import cron_history
        from server.infrastructure.config import load_config

        max_runs = int(load_config().get("cron_max_runs_per_job", 200))
        cron_history.finish_run(
            run_id,
            status=status,
            text=text,
            duration_ms=duration_ms,
            next_run=next_run,
            max_per_job=max_runs,
        )
    except Exception as exc:
        log.warning("cron: failed to record run history: %s", exc)

    if target:
        _emit_cron_event(
            target,
            event_type="cron_fired",
            cron_id=job_id or job_name,
            cron_name=job_name,
            schedule_label_str=schedule_label(sch),
            next_run=next_run,
            text=text,
            status=status,
            run_id=run_id,
            duration_ms=duration_ms,
        )


# ── Scheduler ────────────────────────────────────────────────────────────────


async def init_scheduler():
    global _scheduler, _server_loop
    try:
        _server_loop = asyncio.get_running_loop()
    except RuntimeError:
        _server_loop = None

    # Boot pass: reconcile EVERY 'running' lease regardless of age — the
    # scheduler was down and can't own any, even one opened just before restart.
    try:
        from server import cron_history

        reconciled = cron_history.reconcile_stale(all_running=True)
        if reconciled:
            log.info("cron: reconciled %d interrupted run(s) to failed", reconciled)
    except Exception as exc:
        log.warning("cron: history init failed: %s", exc)

    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        _scheduler = AsyncIOScheduler()
        _scheduler.start()
        jobs = load_cron_jobs()
        # Normalize the on-disk file once so legacy shapes are written forward.
        try:
            save_cron_jobs(jobs)
        except Exception:
            pass
        for job in jobs:
            if job.get("enabled", True):
                _add_job_to_scheduler(job, catch_up=True)
        log.info("Cron scheduler started with %d jobs", len(jobs))
    except ImportError:
        log.info("APScheduler not installed — cron disabled")


def _make_runner(job_id: str):
    def _run(_job_id=job_id):
        def _mark(jobs):
            for j in jobs:
                if j.get("id") == _job_id:
                    j["last_run"] = _utc_now_iso()
                    j["run_count"] = j.get("run_count", 0) + 1
                    # One-shot 'at' jobs disable BEFORE executing so a crash in
                    # the fire→persist window can't re-deliver on restart.
                    if (j.get("schedule") or {}).get("type") == "at":
                        j["enabled"] = False
                        j["fired_at"] = _utc_now_iso()
                    log.info("Cron '%s' triggered (session=%s)", j.get("name"), j.get("session_id"))
                    return dict(j)
            return None

        job = _mutate_jobs(_mark)
        if job is None:
            return
        threading.Thread(target=_execute_cron_prompt, args=(_job_id,), daemon=True).start()

    return _run


def _add_job_to_scheduler(job: dict, *, catch_up: bool = False):
    if not _scheduler:
        return
    if not job.get("enabled", True):
        return
    sch = job.get("schedule") or {}
    # Never re-arm a one-shot that already fired or whose instant has passed.
    if sch.get("type") == "at":
        if job.get("fired_at"):
            return
        try:
            run_at = _parse_dt(sch["run_at"])
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=_zone(sch.get("tz")))
            if run_at <= datetime.now(timezone.utc):
                return
        except Exception:
            return
    try:
        from server.infrastructure.config import load_config

        grace = int(load_config().get("cron_misfire_grace_sec", 3600))
    except Exception:
        grace = 3600
    try:
        trigger = _build_trigger(sch, last_run_iso=job.get("last_run"))
        # Wall-clock catch-up (boot only): a cron job whose fire fell during
        # downtime gets a fresh future-only trigger and would skip it (interval
        # jobs self-heal via their past start_date). Replay the missed fire ONCE
        # before arming the trigger (whose next fire is strictly future, so no
        # double-fire); gated on catch_up so create/update never runs retroactively.
        if catch_up and cron_catch_up_due(sch, job.get("last_run"), grace):
            log.info("cron: catch-up fire for '%s' (missed during downtime)", job.get("name"))
            _make_runner(job["id"])()
        _scheduler.add_job(
            _make_runner(job["id"]),
            trigger=trigger,
            id=job["id"],
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=grace,
        )
    except Exception as e:
        log.error("Failed to schedule job '%s': %s", job.get("name"), e)


def _remove_job_from_scheduler(job_id: str):
    if not _scheduler:
        return
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass


def run_job_now(job_id: str) -> dict:
    """Fire a job off-schedule, right now. Returns a status dict."""
    jobs = load_cron_jobs()
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if not job:
        return {"error": "job not found"}
    with _IN_PROGRESS_LOCK:
        if job_id in _in_progress:
            return {"error": "job is already running"}
    threading.Thread(target=_execute_cron_prompt, args=(job_id,), daemon=True).start()
    return {"started": True, "job_id": job_id}


def on_session_deleted(session_id: str) -> None:
    """Session-delete cascade: disable + flag orphaned every job that owned this
    session (never hard-delete — the user may re-home it), and repoint its run
    history to the inbox so nothing is lost."""
    if not session_id:
        return

    def _orphan(jobs):
        changed = []
        for j in jobs:
            if j.get("session_id") == session_id:
                j["enabled"] = False
                j["orphaned"] = True
                changed.append(j.get("id", ""))
        return changed

    changed = _mutate_jobs(_orphan)
    for jid in changed:
        _remove_job_from_scheduler(jid)
    if changed:
        try:
            from server import cron_history
            from server.infrastructure.sessions import ensure_cron_inbox

            cron_history.repoint_session(session_id, ensure_cron_inbox())
        except Exception as exc:
            log.warning("cron: repoint run history failed: %s", exc)


# ── Public job shape (API) ───────────────────────────────────────────────────


def _job_public(job: dict) -> dict:
    """Enrich a stored job for the API: schedule label, next run, run state,
    and owning-session presence/title."""
    out = dict(job)
    sch = job.get("schedule") or {}
    out["schedule_label"] = schedule_label(sch)
    out["next_run"] = _next_run_iso(job)
    jid = job.get("id", "")
    with _IN_PROGRESS_LOCK:
        running = jid in _in_progress
    if running:
        out["run_state"] = "running"
    else:
        try:
            from server import cron_history

            out["run_state"] = cron_history.last_status(jid)
        except Exception:
            out["run_state"] = None
    sid = job.get("session_id", "")
    try:
        from server.infrastructure.sessions import get_session_meta

        meta = get_session_meta(sid) if sid else None
    except Exception:
        meta = None
    out["session_exists"] = meta is not None
    out["session_title"] = meta.get("title") if meta else None
    return out


# ── Tools exposed to Claude ──────────────────────────────────────────────────

_SCHEDULE_TOOL_SCHEMA = {
    "type": "object",
    "description": (
        "When to run. One of: "
        "{type:'interval', every_minutes:N} for 'every N minutes'; "
        "{type:'cron', hour:H, minute:M, day_of_week:D} for a wall-clock time — "
        "day_of_week is '*' (daily), 'mon-fri' (weekdays), 'sat,sun' (weekends), "
        "a single day like 'mon', or a list 'mon,thu'; hour/minute may be a comma "
        "string for multiple times ('9,16'); "
        "{type:'at', run_at:'YYYY-MM-DDTHH:MM:SS'} for a one-time run. "
        "Examples: 'daily at 9am' -> {type:'cron',hour:9,minute:0}; "
        "'weekdays at noon' -> {type:'cron',hour:12,day_of_week:'mon-fri'}; "
        "'every Monday 8am' -> {type:'cron',hour:8,day_of_week:'mon'}; "
        "'every 30 min' -> {type:'interval',every_minutes:30}. "
        "Timezone is the host system zone unless you pass tz (IANA name)."
    ),
    "properties": {
        "type": {"type": "string", "enum": ["interval", "cron", "at"]},
        "every_minutes": {"type": "number"},
        "hour": {"type": ["integer", "string"]},
        "minute": {"type": ["integer", "string"]},
        "day_of_week": {"type": "string"},
        "run_at": {"type": "string"},
        "tz": {"type": "string", "description": "IANA name; omit to use the host timezone"},
    },
}

CRON_TOOLS = [
    {
        "name": "cron_create",
        "description": (
            "Schedule a recurring or one-time task. Runs the prompt on the given "
            "schedule (wall-clock times supported, e.g. daily at 9am)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique job name"},
                "prompt": {
                    "type": "string",
                    "description": "The instruction to execute on each trigger",
                },
                "schedule": _SCHEDULE_TOOL_SCHEMA,
                "interval_minutes": {
                    "type": "number",
                    "description": "Deprecated shorthand for {type:'interval'}",
                },
            },
            "required": ["name", "prompt"],
        },
    },
    {
        "name": "cron_update",
        "description": "Edit an existing cron job's prompt and/or schedule and/or enabled state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Current job name to edit"},
                "new_name": {"type": "string"},
                "prompt": {"type": "string"},
                "schedule": _SCHEDULE_TOOL_SCHEMA,
                "enabled": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "cron_run",
        "description": "Run a cron job once right now, off its schedule.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Job name to run"}},
            "required": ["name"],
        },
    },
    {
        "name": "cron_list",
        "description": "List all scheduled cron jobs with their schedules and next run.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cron_delete",
        "description": "Delete a scheduled cron job by name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Job name to delete"}},
            "required": ["name"],
        },
    },
]


def _validate_job_model(model: str | None) -> str:
    """Per-job model override: must be a configured Anthropic chat model
    (the unattended loop only speaks the Anthropic InvokeModel API).
    Returns the validated key or '' (use the default fallback chain)."""
    key = (model or "").strip()
    if not key:
        return ""
    from server.infrastructure.config import load_config

    chat_models = load_config().get("chat_models", {})
    resolved = str(chat_models.get(key, ""))
    if "anthropic" not in resolved.lower():
        raise ValueError(
            f"model '{key}' is not a configured Anthropic chat model; "
            "cron jobs support Anthropic models only"
        )
    return key


def _create_job(name, prompt, schedule, session_id, model: str = "") -> dict:
    job = {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "prompt": prompt,
        "schedule": schedule,
        "enabled": True,
        "orphaned": False,
        "schema_version": 2,
        "run_count": 0,
        "created_at": _utc_now_iso(),
        "last_run": None,
        "session_id": session_id,
        "model": model,
    }
    _mutate_jobs(lambda jobs: jobs.append(job))
    _add_job_to_scheduler(job)
    _emit_cron_event(
        session_id,
        event_type="cron_created",
        cron_id=job["id"],
        cron_name=name,
        schedule_label_str=schedule_label(schedule),
        next_run=_next_run_iso(job),
    )
    return job


def execute_cron_tool(tool_name: str, tool_input: dict, session_id: str = "") -> str:
    if tool_name == "cron_create":
        name = tool_input.get("name", "").strip()
        prompt = tool_input.get("prompt", "").strip()
        if not name or not prompt:
            return json.dumps({"error": "name and prompt required"})
        if not session_id:
            return json.dumps({"error": "session_id is required to create a cron job"})
        if any(j.get("name") == name for j in load_cron_jobs()):
            return json.dumps({"error": f"Job '{name}' already exists. Delete it first."})
        try:
            schedule = _validate_schedule(
                tool_input.get("schedule"),
                legacy_interval_minutes=tool_input.get("interval_minutes"),
            )
        except ValueError as e:
            return json.dumps({"error": str(e)})
        try:
            model = _validate_job_model(tool_input.get("model"))
        except ValueError as e:
            return json.dumps({"error": str(e)})
        job = _create_job(name, prompt, schedule, session_id, model=model)
        return json.dumps(
            {
                "created": True,
                "job": _job_public(job),
                "schedule_label": schedule_label(schedule),
                "next_run": _next_run_iso(job),
            }
        )

    if tool_name == "cron_update":
        name = tool_input.get("name", "").strip()
        job = next((j for j in load_cron_jobs() if j.get("name") == name), None)
        if not job:
            return json.dumps({"error": f"No cron job named '{name}'"})
        patch: dict = {}
        if tool_input.get("new_name", "").strip():
            new_name = tool_input["new_name"].strip()
            # Mirror cron_create's uniqueness rule: a rename can't collide with a
            # DIFFERENT job's name (renaming a job to its own name is a no-op).
            if any(
                j.get("name") == new_name and j.get("id") != job["id"] for j in load_cron_jobs()
            ):
                return json.dumps({"error": f"Job '{new_name}' already exists."})
            patch["name"] = new_name
        if tool_input.get("prompt", "").strip():
            patch["prompt"] = tool_input["prompt"].strip()
        if "enabled" in tool_input:
            patch["enabled"] = bool(tool_input["enabled"])
        if tool_input.get("schedule") is not None:
            try:
                patch["schedule"] = _validate_schedule(tool_input["schedule"])
            except ValueError as e:
                return json.dumps({"error": str(e)})
        if tool_input.get("model") is not None:
            try:
                patch["model"] = _validate_job_model(tool_input.get("model"))
            except ValueError as e:
                return json.dumps({"error": str(e)})
        updated = _apply_job_update(job["id"], patch)
        if not updated:
            return json.dumps({"error": "update failed"})
        if session_id:
            _emit_cron_event(
                session_id,
                event_type="cron_updated",
                cron_id=updated["id"],
                cron_name=updated.get("name", name),
                schedule_label_str=schedule_label(updated.get("schedule")),
                next_run=_next_run_iso(updated),
            )
        return json.dumps({"updated": True, "job": _job_public(updated)})

    if tool_name == "cron_run":
        name = tool_input.get("name", "").strip()
        job = next((j for j in load_cron_jobs() if j.get("name") == name), None)
        if not job:
            return json.dumps({"error": f"No cron job named '{name}'"})
        return json.dumps(run_job_now(job["id"]))

    if tool_name == "cron_list":
        return json.dumps(
            {
                "jobs": [_job_public(j) for j in load_cron_jobs()],
                "scheduler_active": _scheduler is not None,
            }
        )

    if tool_name == "cron_delete":
        name = tool_input.get("name", "")
        deleted = _delete_jobs(lambda j: j.get("name") == name)
        if session_id:
            for j in deleted:
                _emit_cron_event(
                    session_id,
                    event_type="cron_deleted",
                    cron_id=j.get("id", ""),
                    cron_name=j.get("name", name),
                    schedule_label_str=schedule_label(j.get("schedule")),
                )
        return json.dumps({"deleted": len(deleted), "name": name})

    return json.dumps({"error": f"Unknown cron tool: {tool_name}"})


def _apply_job_update(job_id: str, patch: dict) -> dict | None:
    """Merge a patch into a job under lock, then atomically reschedule.

    Preserves run_count/last_run/created_at (the user's history). When the
    schedule changes, the interval anchor resets to now (last_run cleared) so
    the new cadence doesn't fire off a stale last_run.
    """

    def _merge(jobs):
        for j in jobs:
            if j.get("id") == job_id:
                schedule_changed = "schedule" in patch and patch["schedule"] != j.get("schedule")
                j.update(patch)
                if schedule_changed:
                    j["last_run"] = None
                    j.pop("fired_at", None)
                return dict(j)
        return None

    updated = _mutate_jobs(_merge)
    if updated is None:
        return None
    _remove_job_from_scheduler(job_id)
    if updated.get("enabled", True):
        _add_job_to_scheduler(updated)
    return updated


def _delete_jobs(pred) -> list[dict]:
    """Remove jobs matching pred; returns the removed jobs."""

    def _do(jobs):
        deleted = [j for j in jobs if pred(j)]
        jobs[:] = [j for j in jobs if not pred(j)]
        return deleted

    deleted = _mutate_jobs(_do)
    for j in deleted:
        _remove_job_from_scheduler(j.get("id", ""))
    # Purge the deleted jobs' run history so their rows don't leak into the
    # recent-runs feed (the sidebar unread badges) forever.
    if deleted:
        try:
            from server import cron_history

            cron_history.delete_job_runs([j.get("id", "") for j in deleted])
        except Exception as exc:
            log.warning("cron: failed to purge run history for deleted jobs: %s", exc)
    return deleted

# HTTP handlers live in the sibling cron_routes module; importing it registers
# them on ``router`` via decorator side-effects. Imported here (after router and
# the engine/job helpers it depends on are defined) so
# ``from server.cron_scheduler import router`` yields a fully-wired router.
from server import cron_routes  # noqa: E402,F401
