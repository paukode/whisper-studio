"""FastAPI HTTP handlers for /api/cron/*.

Split out of server/cron_scheduler.py so that module can stay the scheduler
engine + job model + persistence. These handlers register on the shared
``cron_scheduler.router`` via decorator side-effects; cron_scheduler imports
this module for those effects.

The scheduler singleton (``_scheduler``) and the in-progress run state
(``_in_progress`` / ``_IN_PROGRESS_LOCK``) are read through the ``_cs`` module
alias, not imported by value: ``_scheduler`` is (re)assigned in
``init_scheduler`` and the in-progress set is mutated by the run loop, so a
by-value import would freeze a stale reference.
"""

import json

from fastapi import Request
from fastapi.responses import Response

from server import cron_scheduler as _cs

# Schedule-domain helpers come from their source module; the job/engine
# helpers and the shared router come from cron_scheduler.
from server.cron_schedule import (
    _default_tz_name,
    compute_next_run,
    schedule_label,
)
from server.cron_schedule import (
    validate_schedule as _validate_schedule,
)
from server.cron_scheduler import (
    _add_job_to_scheduler,
    _apply_job_update,
    _at_job_already_fired,
    _delete_jobs,
    _job_public,
    _mutate_jobs,
    _next_run_iso,
    _remove_job_from_scheduler,
    _validate_job_model,
    execute_cron_tool,
    load_cron_jobs,
    router,
    run_job_now,
)


@router.get("")
async def get_cron_jobs():
    return {
        "jobs": [_job_public(j) for j in load_cron_jobs()],
        "scheduler_active": _cs._scheduler is not None and getattr(_cs._scheduler, "running", False),
        "system_timezone": _default_tz_name(),
    }


@router.get("/runs/recent")
async def get_recent_runs(limit: int = 200):
    """Newest runs across all jobs — powers the sidebar unread badges."""
    from server import cron_history

    return {"runs": cron_history.recent_runs(limit)}


@router.post("/preview")
async def preview_schedule(request: Request):
    """Validate a schedule and return its label + next run, no persistence."""
    body = await request.json()
    try:
        sch = _validate_schedule(
            body.get("schedule"), legacy_interval_minutes=body.get("interval_minutes")
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"label": schedule_label(sch), "next_run": compute_next_run(sch), "schedule": sch}


@router.post("")
async def create_cron_job(request: Request):
    body = await request.json()
    # Panel-created jobs may have no active chat session — land their results
    # in the Scheduled Reports inbox rather than rejecting the create.
    session_id = body.get("session_id") or ""
    if not session_id:
        try:
            from server.infrastructure.sessions import ensure_cron_inbox

            session_id = ensure_cron_inbox()
        except Exception:
            pass
    result = json.loads(execute_cron_tool("cron_create", body, session_id=session_id))
    if "error" in result:
        return Response(content=json.dumps(result), status_code=400, media_type="application/json")
    return result


@router.get("/{job_id}")
async def get_cron_job(job_id: str):
    job = next((j for j in load_cron_jobs() if j.get("id") == job_id), None)
    if not job:
        return Response(
            content=json.dumps({"error": "not found"}),
            status_code=404,
            media_type="application/json",
        )
    from server import cron_history

    return {"job": _job_public(job), "runs": cron_history.list_runs(job_id, 50)}


@router.get("/{job_id}/history")
async def get_cron_history(job_id: str, limit: int = 50):
    from server import cron_history

    return {"runs": cron_history.list_runs(job_id, limit)}


@router.put("/{job_id}")
async def update_cron_job(job_id: str, request: Request):
    body = await request.json()
    patch: dict = {}
    if isinstance(body.get("name"), str) and body["name"].strip():
        new_name = body["name"].strip()
        # Same uniqueness rule as cron_create / the cron_update tool: a rename
        # can't take a name already owned by a different job.
        if any(j.get("name") == new_name and j.get("id") != job_id for j in load_cron_jobs()):
            return Response(
                content=json.dumps({"error": f"Job '{new_name}' already exists."}),
                status_code=400,
                media_type="application/json",
            )
        patch["name"] = new_name
    if isinstance(body.get("prompt"), str) and body["prompt"].strip():
        patch["prompt"] = body["prompt"].strip()
    if "enabled" in body:
        patch["enabled"] = bool(body["enabled"])
    if body.get("model") is not None:
        try:
            patch["model"] = _validate_job_model(body.get("model"))
        except ValueError as e:
            return Response(
                content=json.dumps({"error": str(e)}),
                status_code=400,
                media_type="application/json",
            )
    if body.get("schedule") is not None or body.get("interval_minutes") is not None:
        try:
            patch["schedule"] = _validate_schedule(
                body.get("schedule"),
                legacy_interval_minutes=body.get("interval_minutes"),
            )
        except ValueError as e:
            return Response(
                content=json.dumps({"error": str(e)}),
                status_code=400,
                media_type="application/json",
            )
    # Re-homing an orphaned job to a new session clears the orphaned flag.
    if isinstance(body.get("session_id"), str) and body["session_id"].strip():
        patch["session_id"] = body["session_id"].strip()
        patch["orphaned"] = False
    updated = _apply_job_update(job_id, patch)
    if not updated:
        return Response(
            content=json.dumps({"error": "not found"}),
            status_code=404,
            media_type="application/json",
        )
    return {"updated": True, "job": _job_public(updated)}


@router.post("/{job_id}/run")
async def run_cron_job(job_id: str):
    result = run_job_now(job_id)
    if "error" in result:
        code = 404 if result["error"] == "job not found" else 409
        return Response(content=json.dumps(result), status_code=code, media_type="application/json")
    return result


@router.post("/{job_id}/stop")
async def stop_cron_job(job_id: str):
    """Cooperatively stop an in-flight run of this job.

    404 when the job doesn't exist, 409 when it isn't running. The run loop
    observes the flag at round boundaries and between tools (in-flight tool
    waits poll in 1s slices), then exits with status 'stopped'.
    """
    # Live registry FIRST: a job deleted while its run is in flight must
    # still be stoppable (the file no longer lists it, the run doesn't care).
    with _cs._IN_PROGRESS_LOCK:
        running = job_id in _cs._in_progress
    if running:
        from server import cron_events

        requested = cron_events.request_stop(job_id)
        return {"stopping": bool(requested)}
    if not any(j.get("id") == job_id for j in load_cron_jobs()):
        return Response(
            content=json.dumps({"error": "job not found"}),
            status_code=404,
            media_type="application/json",
        )
    return Response(
        content=json.dumps({"error": "job is not running"}),
        status_code=409,
        media_type="application/json",
    )


@router.patch("/{job_id}/toggle")
async def toggle_cron_job(job_id: str):
    refused = {"flag": False}

    def _toggle(jobs):
        for job in jobs:
            if job.get("id") == job_id:
                # Re-enabling a one-shot 'at' job that already fired is a no-op:
                # its instant has passed, so it never re-arms and would only show
                # a stale past next_run. Refuse instead of silently misleading.
                if not job.get("enabled", True) and _at_job_already_fired(job):
                    refused["flag"] = True
                    return dict(job)
                job["enabled"] = not job.get("enabled", True)
                return dict(job)
        return None

    job = _mutate_jobs(_toggle)
    if job is None:
        return Response(
            content=json.dumps({"error": "Job not found"}),
            status_code=404,
            media_type="application/json",
        )
    if refused["flag"]:
        return Response(
            content=json.dumps(
                {
                    "error": "This one-time job already fired and can't be re-enabled. "
                    "Create a new job with a future time.",
                    "job_id": job_id,
                }
            ),
            status_code=409,
            media_type="application/json",
        )
    _remove_job_from_scheduler(job_id)
    if job["enabled"]:
        _add_job_to_scheduler(job)
    return {"job_id": job_id, "enabled": job["enabled"], "next_run": _next_run_iso(job)}


@router.delete("/{job_id}")
async def delete_cron_job(job_id: str):
    deleted = _delete_jobs(lambda j: j.get("id") == job_id)
    if not deleted:
        return Response(
            content=json.dumps({"error": "not found"}),
            status_code=404,
            media_type="application/json",
        )
    return {"deleted": len(deleted), "id": job_id}
