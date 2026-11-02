"""Tests for the wall-clock cron redesign: schedule model, labels, old-format jobs,
next-run projection, and the id-based CRUD/tool surface.

The DB-touching helpers (run history, session meta) are pointed at temp paths or
stubbed so these stay hermetic.
"""

import json
import os
import tempfile
from datetime import UTC

import pytest

import server.cron_routes as CR
import server.cron_schedule as CS
import server.cron_scheduler as C
from server import cron_history

# ── Pure schedule helpers ─────────────────────────────────────────────────────


def test_validate_interval_from_every_minutes():
    sch = C._validate_schedule({"type": "interval", "every_minutes": 30})
    assert sch == {"type": "interval", "seconds": 1800}


def test_validate_interval_floor_10s():
    sch = C._validate_schedule({"type": "interval", "seconds": 3})
    assert sch["seconds"] == 10


def test_validate_cron_fills_defaults_and_tz():
    sch = C._validate_schedule({"type": "cron", "hour": 9})
    assert sch["type"] == "cron"
    assert sch["hour"] == 9 and sch["minute"] == 0
    assert sch["day_of_week"] == "*"
    assert sch["tz"]  # filled from host/default


def test_validate_cron_multi_hour_string():
    sch = C._validate_schedule({"type": "cron", "hour": "9,16", "minute": 0})
    assert sch["hour"] == "9,16"
    assert "09:00 and 16:00" in C.schedule_label(sch)


def test_validate_cron_weekdays():
    sch = C._validate_schedule({"type": "cron", "hour": 12, "day_of_week": "mon-fri"})
    assert sch["day_of_week"] == "mon-fri"
    assert C.schedule_label(sch).startswith("weekdays at 12:00")


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "cron"},  # missing hour
        {"type": "cron", "hour": 99},  # hour out of range
        {"type": "cron", "hour": 9, "day_of_week": "funday"},  # bad day token
        {"type": "cron", "hour": 9, "tz": "Mars/Phobos"},  # bad tz
        {"type": "at", "run_at": "2000-01-01T00:00:00"},  # past
        {"type": "bogus"},  # unknown type
    ],
)
def test_validate_rejects_bad(bad):
    with pytest.raises(ValueError):
        C._validate_schedule(bad)


def test_interval_minutes_is_refused():
    # The old shorthand is no longer accepted: the caller is told to send a
    # schedule instead of getting one guessed for it.
    with pytest.raises(ValueError, match="interval_minutes is no longer accepted"):
        CS.reject_interval_minutes({"interval_minutes": 1440})
    CS.reject_interval_minutes({"schedule": {"type": "interval", "every_minutes": 5}})
    with pytest.raises(ValueError, match="schedule is required"):
        C._validate_schedule(None)


# ── day_of_week range check (fix 2) ───────────────────────────────────────────


def test_validate_cron_numeric_and_named_days_pass():
    # Both the numeric 0-6 form and the three-letter names validate.
    assert (
        C._validate_schedule({"type": "cron", "hour": 9, "day_of_week": "0"})["day_of_week"] == "0"
    )
    assert (
        C._validate_schedule({"type": "cron", "hour": 9, "day_of_week": "6"})["day_of_week"] == "6"
    )
    assert (
        C._validate_schedule({"type": "cron", "hour": 9, "day_of_week": "0-6"})["day_of_week"]
        == "0-6"
    )
    assert (
        C._validate_schedule({"type": "cron", "hour": 9, "day_of_week": "sun"})["day_of_week"]
        == "sun"
    )


@pytest.mark.parametrize("bad_day", ["7", "8", "99", "0,7", "mon,9", "1-7"])
def test_validate_cron_out_of_range_numeric_day_rejected(bad_day):
    # Out-of-range numeric day tokens (APScheduler only accepts 0-6) must fail
    # at validate time, not silently create a job that never fires.
    with pytest.raises(ValueError):
        C._validate_schedule({"type": "cron", "hour": 9, "day_of_week": bad_day})


def test_schedule_label_interval_units():
    assert C.schedule_label({"type": "interval", "seconds": 45}) == "every 45 sec"
    assert C.schedule_label({"type": "interval", "seconds": 1800}) == "every 30 min"
    assert C.schedule_label({"type": "interval", "seconds": 7200}) == "every 2 hr"


def test_schedule_label_single_day():
    sch = {"type": "cron", "hour": 8, "minute": 0, "day_of_week": "mon", "tz": "UTC"}
    assert C.schedule_label(sch).startswith("Mondays at 08:00")


def test_compute_next_run_future_for_daily():
    nxt = C.compute_next_run(
        {"type": "cron", "hour": 9, "minute": 0, "day_of_week": "*", "tz": "UTC"}
    )
    assert nxt is not None


def test_interval_start_resume_anchor():
    from datetime import datetime, timedelta, timezone

    last = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    start = CS._interval_start(600, last)  # 10-min interval, ran 5 min ago
    delta = (start - datetime.now(UTC)).total_seconds()
    assert 250 < delta < 350  # ~5 min out, not immediate


# ── CRUD via the tool/functions, hermetic ─────────────────────────────────────


@pytest.fixture
def temp_cron(monkeypatch):
    d = tempfile.mkdtemp(prefix="cron-test-")
    monkeypatch.setattr(C, "CRON_PATH", os.path.join(d, "cron_jobs.json"))
    monkeypatch.setattr(cron_history, "DB_PATH", os.path.join(d, "hist.db"))
    # No scheduler / no event loop in the unit test — stub the inline emit and
    # session-meta lookup so create/update/delete don't reach for a real DB/loop.
    monkeypatch.setattr(C, "_emit_cron_event", lambda *a, **k: None)
    import server.infrastructure.sessions as S

    monkeypatch.setattr(S, "get_session_meta", lambda sid: None)
    monkeypatch.setattr(S, "ensure_cron_inbox", lambda: "__cron_inbox__")
    # These tests exercise the scheduling logic, not the approval gate (that is
    # covered by test_app_config_floor.py and the cron mode tests). Default the
    # cron tool to the inline path (gate=False, what the Settings UI uses) so a
    # create/update/run applies at once, the way it did before gating.
    _orig_exec = C.execute_cron_tool
    monkeypatch.setattr(
        C,
        "execute_cron_tool",
        lambda tool, args, session_id="", gate=False: _orig_exec(
            tool, args, session_id=session_id, gate=gate
        ),
    )
    return d


def test_create_list_update_toggle_delete(temp_cron):
    # Create a daily wall-clock job.
    res = json.loads(
        C.execute_cron_tool(
            "cron_create",
            {
                "name": "news",
                "prompt": "gather AI news",
                "schedule": {"type": "cron", "hour": 9, "minute": 0},
            },
            session_id="sess-1",
        )
    )
    assert res["created"] is True
    jid = res["job"]["id"]
    assert res["job"]["schedule_label"].startswith("every day at 09:00")
    assert res["job"]["next_run"]

    # List reflects it, enriched.
    listing = json.loads(C.execute_cron_tool("cron_list", {}))
    assert len(listing["jobs"]) == 1
    assert listing["jobs"][0]["schedule"]["type"] == "cron"

    # Update the schedule to weekdays at noon.
    upd = json.loads(
        C.execute_cron_tool(
            "cron_update",
            {
                "name": "news",
                "schedule": {"type": "cron", "hour": 12, "day_of_week": "mon-fri"},
            },
            session_id="sess-1",
        )
    )
    assert upd["updated"] is True
    assert upd["job"]["schedule"]["day_of_week"] == "mon-fri"
    assert upd["job"]["last_run"] is None  # cadence changed → anchor reset

    # Disabling clears next_run.
    C._apply_job_update(jid, {"enabled": False})
    disabled = C._job_public(C.load_cron_jobs()[0])
    assert disabled["enabled"] is False
    assert disabled["next_run"] is None

    # Delete by name.
    dele = json.loads(C.execute_cron_tool("cron_delete", {"name": "news"}))
    assert dele["deleted"] == 1
    assert C.load_cron_jobs() == []


def test_on_session_deleted_orphans_not_deletes(temp_cron):
    json.loads(
        C.execute_cron_tool(
            "cron_create",
            {
                "name": "watch",
                "prompt": "x",
                "schedule": {"type": "interval", "every_minutes": 10},
            },
            session_id="doomed",
        )
    )
    C.on_session_deleted("doomed")
    jobs = C.load_cron_jobs()
    assert len(jobs) == 1  # not hard-deleted
    assert jobs[0]["enabled"] is False  # disabled
    assert jobs[0]["orphaned"] is True  # flagged, re-homeable


def test_duplicate_name_rejected(temp_cron):
    C.execute_cron_tool(
        "cron_create",
        {
            "name": "dup",
            "prompt": "x",
            "schedule": {"type": "interval", "every_minutes": 5},
        },
        session_id="s",
    )
    res = json.loads(
        C.execute_cron_tool(
            "cron_create",
            {
                "name": "dup",
                "prompt": "y",
                "schedule": {"type": "interval", "every_minutes": 5},
            },
            session_id="s",
        )
    )
    assert "error" in res


def test_at_job_disabled_after_migrationless_shape(temp_cron):
    # A future one-shot validates and stores tz-aware run_at.
    res = json.loads(
        C.execute_cron_tool(
            "cron_create",
            {
                "name": "once",
                "prompt": "x",
                "schedule": {"type": "at", "run_at": "2099-01-01T09:00:00"},
            },
            session_id="s",
        )
    )
    assert res["created"] is True
    assert res["job"]["schedule"]["type"] == "at"


# ── Wall-clock catch-up for downtime-missed fires (fix 3) ─────────────────────


def test_most_recent_fire_before_returns_last_fire_in_window():
    from datetime import datetime, timedelta, timezone

    from apscheduler.triggers.cron import CronTrigger

    trig = CronTrigger(hour=9, minute=0, day_of_week="*", timezone=UTC)
    now = datetime(2026, 7, 15, 9, 30, tzinfo=UTC)
    # Window opens an hour before now → the 09:00 fire is the most recent one.
    missed = CS._most_recent_fire_before(trig, now, now - timedelta(hours=1))
    assert missed == datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


def test_most_recent_fire_before_none_when_no_fire_in_window():
    from datetime import datetime, timezone

    from apscheduler.triggers.cron import CronTrigger

    trig = CronTrigger(hour=9, minute=0, day_of_week="*", timezone=UTC)
    now = datetime(2026, 7, 15, 9, 30, tzinfo=UTC)
    # Window opens AFTER the 09:00 fire → nothing to catch up on.
    floor = datetime(2026, 7, 15, 9, 15, tzinfo=UTC)
    assert CS._most_recent_fire_before(trig, now, floor) is None


def test_cron_catch_up_due_when_fire_missed_after_last_run():
    from datetime import datetime, timedelta, timezone

    # A cron that fires every minute: the most recent fire before now is always
    # <60s ago (inside the 1h grace window), independent of the wall clock.
    sch = {"type": "cron", "hour": "*", "minute": "*", "day_of_week": "*", "tz": "UTC"}
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    assert CS.cron_catch_up_due(sch, stale, 3600) is True


def test_cron_catch_up_not_due_when_recently_run():
    from datetime import datetime, timezone

    # A daily cron's most recent fire is strictly in the past; last_run=now is
    # newer than it, so nothing is replayed.
    sch = {"type": "cron", "hour": 9, "minute": 0, "day_of_week": "*", "tz": "UTC"}
    now_iso = datetime.now(UTC).isoformat()
    assert CS.cron_catch_up_due(sch, now_iso, 3600) is False


def test_cron_catch_up_not_due_for_never_run_or_interval():
    # Never-run job → no catch-up (mirrors interval self-heal, which never
    # boot-fires a never-run job). Interval jobs are handled by their trigger.
    sch = {"type": "cron", "hour": "*", "minute": "*", "day_of_week": "*", "tz": "UTC"}
    assert CS.cron_catch_up_due(sch, None, 3600) is False
    assert (
        CS.cron_catch_up_due({"type": "interval", "seconds": 60}, "2000-01-01T00:00:00Z", 3600)
        is False
    )


# ── Boot reconcile of interrupted run leases (fix 4) ──────────────────────────


def test_reconcile_all_running_at_boot_fails_fresh_lease(monkeypatch, tmp_path):
    from server import cron_history as H

    monkeypatch.setattr(H, "DB_PATH", str(tmp_path / "hist.db"))
    # A lease opened seconds ago.
    H.start_run("run-fresh", "job-1", "job", "sess")
    # The boot pass reconciles EVERY running row regardless of age.
    assert H.reconcile_interrupted() == 1
    runs = H.list_runs("job-1")
    assert runs[0]["status"] == "failed"
    assert "[interrupted]" in (runs[0]["text"] or "")


# ── Fired one-shot 'at' job never arms / can't be re-enabled (fix 2) ──────────


def _mark_at_job_fired(job_id: str):
    """Flip a stored 'at' job into the post-fire state _make_runner leaves:
    disabled, fired_at + last_run set, and run_at now in the past."""

    def _mut(jobs):
        for j in jobs:
            if j.get("id") == job_id:
                j["enabled"] = False
                j["fired_at"] = "2000-01-01T00:00:00Z"
                j["last_run"] = "2000-01-01T00:00:00Z"
                j["schedule"]["run_at"] = "2000-01-01T09:00:00+00:00"
        return None

    C._mutate_jobs(_mut)


def test_fired_at_job_next_run_is_none(temp_cron):
    res = json.loads(
        C.execute_cron_tool(
            "cron_create",
            {
                "name": "oneshot",
                "prompt": "x",
                "schedule": {"type": "at", "run_at": "2099-01-01T09:00:00"},
            },
            session_id="s",
        )
    )
    jid = res["job"]["id"]
    _mark_at_job_fired(jid)

    job = C.load_cron_jobs()[0]
    # Disabled → None (unchanged behaviour).
    assert C._next_run_iso(job) is None
    # Even forced back to enabled, the projection is in the past → None, so the
    # UI never counts down toward a fire that will never happen.
    assert C._next_run_iso(dict(job, enabled=True)) is None


def test_toggle_refuses_to_reenable_fired_at_job(temp_cron):
    import asyncio

    from fastapi.responses import Response

    res = json.loads(
        C.execute_cron_tool(
            "cron_create",
            {
                "name": "oneshot2",
                "prompt": "x",
                "schedule": {"type": "at", "run_at": "2099-01-01T09:00:00"},
            },
            session_id="s",
        )
    )
    jid = res["job"]["id"]
    _mark_at_job_fired(jid)

    resp = asyncio.run(CR.toggle_cron_job(jid))
    # Refused with 409; the job stays disabled (never re-armed).
    assert isinstance(resp, Response)
    assert resp.status_code == 409
    assert C.load_cron_jobs()[0]["enabled"] is False


# ── Rename must honour name uniqueness (fix 3) ────────────────────────────────


def _create_interval_job(name: str) -> str:
    res = json.loads(
        C.execute_cron_tool(
            "cron_create",
            {"name": name, "prompt": "x", "schedule": {"type": "interval", "every_minutes": 5}},
            session_id="s",
        )
    )
    return res["job"]["id"]


def test_cron_update_rename_to_existing_name_rejected(temp_cron):
    _create_interval_job("alpha")
    _create_interval_job("beta")

    # Renaming beta → alpha collides with a different job → rejected.
    res = json.loads(
        C.execute_cron_tool("cron_update", {"name": "beta", "new_name": "alpha"}, session_id="s")
    )
    assert "error" in res
    assert sorted(j["name"] for j in C.load_cron_jobs()) == ["alpha", "beta"]

    # Renaming a job to its OWN name is not a collision (same id) → allowed.
    ok = json.loads(
        C.execute_cron_tool("cron_update", {"name": "beta", "new_name": "beta"}, session_id="s")
    )
    assert ok.get("updated") is True


def test_put_rename_to_existing_name_rejected(temp_cron):
    import asyncio

    from fastapi.responses import Response

    _create_interval_job("alpha")
    beta_id = _create_interval_job("beta")

    class _Req:
        def __init__(self, body):
            self._b = body

        async def json(self):
            return self._b

    resp = asyncio.run(CR.update_cron_job(beta_id, _Req({"name": "alpha"})))
    assert isinstance(resp, Response)
    assert resp.status_code == 400
    assert sorted(j["name"] for j in C.load_cron_jobs()) == ["alpha", "beta"]


# ── Deleting a job purges its run history (fix 4) ─────────────────────────────


def test_delete_job_purges_run_history(temp_cron):
    from server import cron_history as H

    jid = _create_interval_job("purge-me")
    # Record two finished runs for it.
    for rid in ("r1", "r2"):
        H.start_run(rid, jid, "purge-me", "s")
        H.finish_run(rid, status="ok", text="done")
    assert len(H.list_runs(jid)) == 2
    assert any(r["job_id"] == jid for r in H.recent_runs())

    dele = json.loads(C.execute_cron_tool("cron_delete", {"name": "purge-me"}))
    assert dele["deleted"] == 1

    # History rows are gone from both the per-job and recent feeds.
    assert H.list_runs(jid) == []
    assert not any(r["job_id"] == jid for r in H.recent_runs())


# ── Old-format jobs (interval_minutes, no schedule) are never run ─────────────

_OLD_JOB = {"id": "old1", "name": "legacy", "prompt": "x", "interval_minutes": 60, "enabled": True}


def _store(jobs) -> bytes:
    """Store jobs the way the app writes the file; return the file's bytes."""
    C.save_cron_jobs(jobs)
    return _stored_bytes()


def _stored_bytes() -> bytes:
    with open(C.CRON_PATH, "rb") as f:
        return f.read()


class _Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


class _FakeScheduler:
    """Stands in for APScheduler and records every job it is asked to arm."""

    def __init__(self):
        self.added = []

    def start(self):
        pass

    def add_job(self, *a, **k):
        self.added.append(k)

    def get_job(self, *a, **k):
        return None

    def remove_job(self, *a, **k):
        pass


@pytest.fixture
def notices(monkeypatch):
    import server.notifications as N

    seen = []
    monkeypatch.setattr(N, "old_format_notice", lambda **kw: seen.append(kw))
    return seen


def test_an_old_job_is_listed_as_stored_and_never_armed(temp_cron, monkeypatch):
    before = _store([dict(_OLD_JOB)])
    fake = _FakeScheduler()
    monkeypatch.setattr(C, "_scheduler", fake)

    [job] = C.load_cron_jobs()
    assert job == _OLD_JOB  # no schedule filled in
    public = C._job_public(job)
    assert public["schedule_label"] == "not scheduled (old format)"
    assert public["next_run"] is None
    # Shown off, since it never runs, though the file says enabled.
    assert public["enabled"] is False and public["old_format"] is True
    C._add_job_to_scheduler(job, catch_up=True)
    assert fake.added == []
    assert _stored_bytes() == before


def test_each_old_job_gets_one_notice(temp_cron, notices):
    fresh = {
        "id": "n1",
        "name": "fresh",
        "prompt": "x",
        "schedule": {"type": "interval", "seconds": 60},
    }
    before = _store([dict(_OLD_JOB), fresh])

    C._report_old_jobs(C.load_cron_jobs())

    [notice] = notices
    assert notice["title"] == C.OLD_JOB_NOTICE_TITLE
    assert "'legacy'" in notice["message"] and "interval_minutes" in notice["message"]
    assert _stored_bytes() == before


def test_startup_reports_old_jobs_and_leaves_the_file_alone(temp_cron, monkeypatch, notices):
    import asyncio

    import apscheduler.schedulers.asyncio as aps

    # monkeypatch restores both globals that init_scheduler sets.
    monkeypatch.setattr(C, "_scheduler", None)
    monkeypatch.setattr(C, "_server_loop", None)
    monkeypatch.setattr(aps, "AsyncIOScheduler", _FakeScheduler)
    before = _store([dict(_OLD_JOB)])

    asyncio.run(C.init_scheduler())

    assert C._scheduler.added == []
    assert [n["title"] for n in notices] == [C.OLD_JOB_NOTICE_TITLE]
    assert _stored_bytes() == before


def test_an_old_job_cannot_be_switched_on_run_or_changed_without_a_schedule(temp_cron, monkeypatch):
    import asyncio

    from fastapi.responses import Response

    monkeypatch.setattr(C, "_scheduler", _FakeScheduler())
    before = _store([dict(_OLD_JOB, enabled=False)])

    resp = asyncio.run(CR.toggle_cron_job("old1"))
    assert isinstance(resp, Response) and resp.status_code == 409
    assert json.loads(resp.body)["error"] == C.OLD_JOB_ERROR

    resp = asyncio.run(CR.update_cron_job("old1", _Req({"enabled": True})))
    assert isinstance(resp, Response) and resp.status_code == 409

    resp = asyncio.run(CR.run_cron_job("old1"))
    assert resp.status_code == 409
    assert C.run_job_now("old1") == {"error": C.OLD_JOB_ERROR}

    res = json.loads(
        C.execute_cron_tool("cron_update", {"name": "legacy", "enabled": True}, session_id="s")
    )
    assert res == {"error": C.OLD_JOB_ERROR}
    # Refused before any approval card, not after the user approves it.
    res = json.loads(C.execute_cron_tool("cron_run", {"name": "legacy"}, session_id="s", gate=True))
    assert res == {"error": C.OLD_JOB_ERROR}
    assert _stored_bytes() == before


def test_giving_an_old_job_a_schedule_makes_it_a_normal_job(temp_cron, monkeypatch):
    import asyncio

    fake = _FakeScheduler()
    monkeypatch.setattr(C, "_scheduler", fake)
    _store([dict(_OLD_JOB)])

    body = {"schedule": {"type": "interval", "every_minutes": 60}}
    resp = asyncio.run(CR.update_cron_job("old1", _Req(body)))
    assert resp["updated"] is True
    assert resp["job"]["schedule"] == {"type": "interval", "seconds": 3600}
    assert resp["job"]["schedule_label"] != "not scheduled (old format)"
    assert len(fake.added) == 1


def test_deleting_the_session_leaves_an_old_job_as_stored(temp_cron):
    # Written the way an older app or a hand edit might, not the way this app
    # formats it, so a rewrite would show.
    with open(C.CRON_PATH, "w") as f:
        f.write(json.dumps({"jobs": [dict(_OLD_JOB, session_id="doomed")]}, separators=(",", ":")))
    before = _stored_bytes()
    C.on_session_deleted("doomed")
    assert _stored_bytes() == before


def test_interval_minutes_is_refused_by_the_tool_and_the_routes(temp_cron):
    import asyncio

    from fastapi.responses import Response

    res = json.loads(
        C.execute_cron_tool(
            "cron_create", {"name": "n", "prompt": "x", "interval_minutes": 5}, session_id="s"
        )
    )
    assert "interval_minutes is no longer accepted" in res["error"]
    assert C.load_cron_jobs() == []

    jid = _create_interval_job("alpha")
    res = json.loads(
        C.execute_cron_tool("cron_update", {"name": "alpha", "interval_minutes": 5}, session_id="s")
    )
    assert "interval_minutes is no longer accepted" in res["error"]
    resp = asyncio.run(CR.update_cron_job(jid, _Req({"interval_minutes": 5})))
    assert isinstance(resp, Response) and resp.status_code == 400
    preview = asyncio.run(CR.preview_schedule(_Req({"interval_minutes": 5})))
    assert "interval_minutes is no longer accepted" in preview["error"]
    assert C.load_cron_jobs()[0]["schedule"] == {"type": "interval", "seconds": 300}


def test_an_unreadable_job_file_is_never_overwritten(temp_cron, monkeypatch):
    import asyncio

    import apscheduler.schedulers.asyncio as aps

    with open(C.CRON_PATH, "w") as f:
        f.write('{"jobs": [ {"name": "half-typed"')
    before = _stored_bytes()
    monkeypatch.setattr(C, "_scheduler", None)
    monkeypatch.setattr(C, "_server_loop", None)
    monkeypatch.setattr(aps, "AsyncIOScheduler", _FakeScheduler)

    asyncio.run(C.init_scheduler())  # boot goes on with nothing armed
    assert C._scheduler.added == []
    with pytest.raises(C.CronJobsUnreadable):
        C.load_cron_jobs()
    with pytest.raises(C.CronJobsUnreadable):
        C._mutate_jobs(lambda jobs: jobs.clear())
    assert _stored_bytes() == before


def test_an_unknown_timezone_is_refused_not_swapped_for_another():
    with pytest.raises(ValueError, match="unknown timezone 'Not/AZone'"):
        CS._zone("Not/AZone")
    with pytest.raises(ValueError, match="unknown timezone"):
        CS.validate_schedule({"type": "at", "run_at": "2099-01-01T09:00:00", "tz": "Not/AZone"})
