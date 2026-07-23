"""Schedule domain logic for the cron scheduler.

Pure-ish helpers with no scheduler/loop state: timezone resolution, the
discriminated schedule union (interval | cron | at) with validation and
normalization, human-readable labels, APScheduler trigger construction, and
next-run projection. Kept separate from server/cron_scheduler.py so the runtime
(lifecycle, execution, delivery, routes) stays under the file-size budget.
"""

import os
from datetime import datetime, timedelta, timezone

# ── Timezone ─────────────────────────────────────────────────────────────────


def _system_tz_name() -> str:
    """The host's IANA timezone name (e.g. 'America/New_York').

    Resolves from $TZ, then the /etc/localtime symlink (macOS + Linux), then
    falls back to UTC. No third-party dependency.
    """
    tz = (os.environ.get("TZ") or "").strip()
    if tz:
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
        marker = "zoneinfo/"
        if marker in target:
            return target.split(marker, 1)[1]
    except Exception:
        pass
    return "UTC"


def _default_tz_name() -> str:
    """Config override (cron_timezone) if set, else the host system zone."""
    try:
        from server.infrastructure.config import load_config

        cfg = (load_config().get("cron_timezone") or "").strip()
        if cfg:
            return cfg
    except Exception:
        pass
    return _system_tz_name()


def _zone(name: str | None):
    """A ZoneInfo for name (or the default zone), UTC on failure."""
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(name or _default_tz_name())
    except Exception:
        try:
            return ZoneInfo(_system_tz_name())
        except Exception:
            return timezone.utc


def _parse_dt(s: str) -> datetime:
    """Parse an ISO string, tolerating a trailing 'Z'."""
    s = (s or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


# ── Schedule: validation, labels, triggers, next-run ─────────────────────────

_WEEKDAY_TOKENS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_DAY_FULL = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}


def _norm_time_field(value, lo: int, hi: int, field: str) -> int | str:
    """Normalize an hour/minute that may be an int or a comma list string
    ('9,16'). Validates each token in [lo, hi]. Returns int for a single
    value, str for a multi-value list (APScheduler accepts both)."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    if isinstance(value, (int, float)):
        v = int(value)
        if not lo <= v <= hi:
            raise ValueError(f"{field} {v} out of range {lo}-{hi}")
        return v
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if not parts:
            raise ValueError(f"{field} is empty")
        out = []
        for p in parts:
            if not p.lstrip("-").isdigit():
                raise ValueError(f"{field} token '{p}' is not a number")
            iv = int(p)
            if not lo <= iv <= hi:
                raise ValueError(f"{field} {iv} out of range {lo}-{hi}")
            out.append(iv)
        return str(out[0]) if len(out) == 1 else ",".join(str(x) for x in out)
    raise ValueError(f"{field} must be a number or comma list")


def _norm_day_of_week(value) -> str:
    """Validate a day_of_week token against APScheduler's grammar.

    APScheduler weekdays are the three-letter names or the numbers 0-6
    (mon-sun). A numeric token outside that range (e.g. '7') is NOT accepted by
    the CronTrigger, so left unchecked it builds a job that reports success at
    create time yet never fires. Range-check numeric tokens here so it surfaces
    at create/update, exactly like the hour/minute range checks.
    """
    if value is None:
        return "*"
    dow = str(value).strip().lower()
    if dow in ("", "*", "daily", "every day"):
        return "*"
    # Accept ranges (mon-fri), lists (mon,wed), and single days.
    tokens = dow.replace("-", ",").split(",")
    for t in tokens:
        t = t.strip()
        if not t:
            continue
        if t.isdigit():
            iv = int(t)
            if not 0 <= iv <= 6:
                raise ValueError(f"day_of_week {iv} out of range 0-6")
        elif t not in _WEEKDAY_TOKENS:
            raise ValueError(f"invalid day_of_week token '{t}'")
    return dow


def validate_schedule(raw: dict | None, *, legacy_interval_minutes=None) -> dict:
    """Return a normalized schedule dict or raise ValueError.

    Fills tz from the default (host) zone when the caller omits it. Accepts a
    bare legacy ``interval_minutes`` (from the old tool/API shape) as a fallback
    so nothing in flight breaks.
    """
    if not isinstance(raw, dict):
        if legacy_interval_minutes is not None:
            secs = max(10, int(float(legacy_interval_minutes) * 60))
            return {"type": "interval", "seconds": secs}
        raise ValueError("schedule is required")

    stype = raw.get("type", "interval")

    if stype == "interval":
        if raw.get("every_minutes") is not None:
            secs = int(float(raw["every_minutes"]) * 60)
        elif raw.get("seconds") is not None:
            secs = int(float(raw["seconds"]))
        elif legacy_interval_minutes is not None:
            secs = int(float(legacy_interval_minutes) * 60)
        else:
            raise ValueError("interval schedule needs every_minutes or seconds")
        return {"type": "interval", "seconds": max(10, secs)}

    if stype == "cron":
        if raw.get("hour") is None:
            raise ValueError("cron schedule needs an hour")
        hour = _norm_time_field(raw.get("hour"), 0, 23, "hour")
        minute = _norm_time_field(raw.get("minute", 0), 0, 59, "minute")
        dow = _norm_day_of_week(raw.get("day_of_week"))
        tz = (raw.get("tz") or "").strip() or _default_tz_name()
        # Validate the zone eagerly so a bad name errors at create time.
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(tz)
        except Exception:
            raise ValueError(f"unknown timezone '{tz}'") from None
        return {"type": "cron", "hour": hour, "minute": minute, "day_of_week": dow, "tz": tz}

    if stype == "at":
        run_at_raw = raw.get("run_at")
        if not run_at_raw:
            raise ValueError("'at' schedule needs run_at")
        tz = (raw.get("tz") or "").strip() or _default_tz_name()
        try:
            run_at = _parse_dt(run_at_raw)
        except Exception:
            raise ValueError(f"could not parse run_at '{run_at_raw}'") from None
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=_zone(tz))
        if run_at <= datetime.now(timezone.utc):
            raise ValueError("run_at is in the past")
        return {"type": "at", "run_at": run_at.isoformat(), "tz": tz}

    raise ValueError(f"unknown schedule type '{stype}'")


def _fmt_hm(hour, minute) -> str:
    """Render hour(s):minute for a label. Multi-hour → 'HH:MM and HH:MM'."""
    mins = str(minute).split(",")
    m0 = int(mins[0]) if mins and mins[0].lstrip("-").isdigit() else 0
    hours = [int(h) for h in str(hour).split(",") if h.lstrip("-").isdigit()]
    if not hours:
        hours = [0]
    times = [f"{h:02d}:{m0:02d}" for h in hours]
    if len(times) == 1:
        return times[0]
    return " and ".join(times)


def _fmt_days(dow: str) -> str:
    dow = (dow or "*").lower()
    if dow == "*":
        return "every day"
    if dow == "mon-fri":
        return "weekdays"
    if dow in ("sat,sun", "sat-sun"):
        return "weekends"
    tokens = [t.strip() for t in dow.split(",") if t.strip()]
    if len(tokens) == 1 and tokens[0] in _DAY_FULL:
        return f"{_DAY_FULL[tokens[0]]}s"
    named = [_DAY_FULL.get(t, t)[:3] for t in tokens]
    return ", ".join(named) if named else dow


def schedule_label(sch: dict | None) -> str:
    """Human-readable schedule string reused by tools, API, panel, and cards."""
    if not isinstance(sch, dict):
        return ""
    stype = sch.get("type", "interval")
    if stype == "interval":
        secs = int(sch.get("seconds", 3600))
        if secs < 60:
            return f"every {secs} sec"
        mins = secs / 60
        if mins < 60:
            m = int(mins) if mins == int(mins) else round(mins, 1)
            return f"every {m} min"
        hrs = mins / 60
        h = int(hrs) if hrs == int(hrs) else round(hrs, 1)
        return f"every {h} hr"
    if stype == "cron":
        when = _fmt_hm(sch.get("hour", 0), sch.get("minute", 0))
        days = _fmt_days(sch.get("day_of_week", "*"))
        tz = sch.get("tz") or ""
        base = f"{days} at {when}"
        return f"{base} · {tz}" if tz else base
    if stype == "at":
        try:
            dt = _parse_dt(sch["run_at"])
            return "once on " + dt.strftime("%b %d, %Y at %H:%M")
        except Exception:
            return "once (scheduled)"
    return ""


def _interval_start(seconds: int, last_run_iso: str | None) -> datetime:
    """Aware start_date so an interval never fires immediately on create/resume.

    A 10-min job whose last_run was 5 min ago resumes 5 min from now, not now.
    Never-run jobs anchor at now+seconds.
    """
    now = datetime.now(timezone.utc)
    floor = now + timedelta(seconds=15)
    if last_run_iso:
        try:
            last = _parse_dt(last_run_iso)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            return max(floor, last + timedelta(seconds=seconds))
        except Exception:
            pass
    return now + timedelta(seconds=seconds)


def build_trigger(sch: dict, *, last_run_iso: str | None):
    """Construct the APScheduler trigger for a schedule. Imported lazily so the
    module still loads (cron disabled) when APScheduler is absent."""
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    stype = sch.get("type", "interval")
    if stype == "cron":
        return CronTrigger(
            minute=sch.get("minute", 0),
            hour=sch.get("hour", 0),
            day_of_week=sch.get("day_of_week", "*"),
            timezone=_zone(sch.get("tz")),
        )
    if stype == "at":
        tz = _zone(sch.get("tz"))
        run_at = _parse_dt(sch["run_at"])
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=tz)
        return DateTrigger(run_date=run_at, timezone=tz)
    seconds = max(10, int(sch.get("seconds", 3600)))
    return IntervalTrigger(seconds=seconds, start_date=_interval_start(seconds, last_run_iso))


def _most_recent_fire_before(trigger, now: datetime, floor: datetime):
    """Walk ``trigger`` forward from ``floor`` and return the last fire time
    strictly before ``now`` (or None if it never fires in ``[floor, now)``).

    Bounded: the caller passes a ``floor`` no earlier than ``now - grace``, so
    the loop steps at most once per scheduled fire inside the grace window.
    """
    prev = None
    cursor = floor
    while cursor < now:
        nxt = trigger.get_next_fire_time(None, cursor)
        if nxt is None or nxt >= now:
            break
        prev = nxt
        cursor = nxt + timedelta(seconds=1)
    return prev


def cron_catch_up_due(sch: dict, last_run_iso: str | None, grace_sec: int) -> bool:
    """True when a wall-clock (cron) job has a scheduled fire that fell during
    server downtime and should be replayed once at startup.

    Interval jobs self-heal (their past ``start_date`` + misfire grace fire one
    coalesced catch-up run on boot); cron jobs get a fresh future-only trigger
    and would otherwise silently skip the missed fire. A fire qualifies when it
    is the most recent one before now, lies within ``grace_sec`` of now, and is
    newer than the job's ``last_run`` (so a fire already executed never repeats).

    Mirrors interval self-heal exactly, including the never-run case: an
    interval job with no ``last_run`` anchors at ``now + interval`` (no boot
    fire), so a cron job that has never run gets no catch-up either — it just
    waits for its next scheduled fire.
    """
    if sch.get("type") != "cron":
        return False
    if not last_run_iso:
        return False
    try:
        last = _parse_dt(last_run_iso)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    now = datetime.now(timezone.utc)
    floor = now - timedelta(seconds=max(0, int(grace_sec)))
    trigger = build_trigger(sch, last_run_iso=None)
    missed = _most_recent_fire_before(trigger, now, floor)
    if missed is None:
        return False
    return missed > last


def compute_next_run(sch: dict | None) -> str | None:
    """Project the next fire time from the schedule alone (no live scheduler).

    Used as the authoritative source for the UI countdown and as the cold
    fallback when the scheduler isn't running.
    """
    if not isinstance(sch, dict):
        return None
    try:
        trigger = build_trigger(sch, last_run_iso=None)
        now = datetime.now(_zone(sch.get("tz")))
        nxt = trigger.get_next_fire_time(None, now)
        return nxt.isoformat() if nxt else None
    except Exception:
        return None
