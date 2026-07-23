"""Workflow run manager — the registry and lifecycle owner.

Rows live in ``workflow_runs`` (sessions.db, migration 009); the immutable
script snapshot + journal live under ``data_root()/workflows/runs/<run_id>/``.
A run is driven detached on the server loop via async_tasks.spawn, its row
updated on completion, and its outcome published on the per-run event_bus
channel ``workflow:{run_id}`` so a reloaded session can re-attach. Boot
reconcile flips orphaned ``running`` rows to ``stale`` (their journal stays
resumable), mirroring cron_history.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

from server.workflows import journal as jnl
from server.workflows.runtime import WorkflowRun

log = logging.getLogger("whisper-studio")

# Live in-process runs, keyed by run_id.
_live: dict[str, WorkflowRun] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn():
    from server.infrastructure.sessions import _get_conn

    return _get_conn()


def _ensure_table(conn) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_runs'"
    ).fetchone()
    if not row:
        import importlib

        importlib.import_module("server.migrations.009_add_workflow_runs").migrate(conn)


def _publish(run_id: str, session_id: str, event: dict) -> None:
    try:
        from server.agents.event_bus import event_bus

        event_bus.publish(f"workflow:{run_id}", {"type": "workflow_event", **event})
        if session_id:
            event_bus.publish(session_id, {"type": "workflow_event", "run_id": run_id, **event})
    except Exception as e:
        log.debug("workflow publish failed: %s", e)


def start_run(
    source: str,
    *,
    args=None,
    session_id: str = "",
    model_key: str = "",
    model_id: str = "",
    effort_label: str | None = None,
    budget_usd: float | None = None,
    phases: list | None = None,
    name: str = "",
    resume_from: str = "",
    agent_runner=None,
) -> str:
    """Register + launch a run detached; returns its run_id immediately."""
    from server.infrastructure.async_tasks import spawn

    run_id = uuid.uuid4().hex[:12]

    # Immutable snapshot so a later saved-script edit never confuses a resume.
    d = jnl.run_dir(run_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "workflow.mjs"), "w", encoding="utf-8") as f:
        f.write(source)

    with _conn() as conn:
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO workflow_runs (run_id, name, session_id, status, phases_json, "
            "args_json, model_key, budget_usd, resumed_from, started_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                name,
                session_id,
                "running",
                json.dumps(phases or []),
                json.dumps(args),
                model_key,
                budget_usd,
                resume_from,
                _now(),
            ),
        )

    resume_cache = jnl.load_resume_cache(resume_from) if resume_from else {}
    journal = jnl.Journal(run_id)
    journal.run_meta({"name": name, "phases": phases or [], "resumed_from": resume_from})

    run = WorkflowRun(
        run_id,
        source,
        args=args,
        session_id=session_id,
        model_id=model_id,
        model_key=model_key,
        effort_label=effort_label,
        budget_usd=budget_usd,
        resume_cache=resume_cache,
        journal=journal,
        agent_runner=agent_runner,
        nested_runner=_make_nested_runner(session_id, model_key, model_id, effort_label),
        on_event=lambda ev: _publish(run_id, session_id, ev),
    )
    _live[run_id] = run
    _schedule(_drive(run), run_id, spawn)
    return run_id


def _schedule(coro, run_id: str, spawn) -> None:
    """Run the drive coroutine on the server loop, whether we're called from the
    loop (chat/tests) or a worker-thread tool executor (route_tool offloads
    executors to a threadpool → no running loop here)."""
    import asyncio

    try:
        asyncio.get_running_loop()
        spawn(coro, name=f"workflow-{run_id}")
        return
    except RuntimeError:
        pass
    loop = _server_loop()
    if loop is None:
        # No captured loop (early boot / tests without the service): run inline.
        try:
            asyncio.run(coro)
        except Exception as e:  # noqa: BLE001
            log.error("workflow %s inline run failed: %s", run_id, e)
        return
    asyncio.run_coroutine_threadsafe(coro, loop)


def _server_loop():
    try:
        from server.tasks import events

        return events._server_loop
    except Exception:
        return None


def _make_nested_runner(session_id, model_key, model_id, effort_label):
    async def nested(name, args, parent):
        loaded = _load_trusted_saved(name)
        if not loaded:
            return {"status": "failed", "error": f"unknown or untrusted workflow: {name}"}
        # Depth-1 child inherits the parent's REMAINING budget so nested spend
        # can't exceed the parent's cap; its usage is merged back into the parent.
        child_budget = None
        if parent.budget_usd is not None:
            child_budget = max(0.0, parent.budget_usd - parent.cost_usd)
        child_id = uuid.uuid4().hex[:12]
        # Give the nested run its own row so it is queryable and its journal dir
        # isn't an orphan; namespace the name under the parent.
        with _conn() as conn:
            _ensure_table(conn)
            conn.execute(
                "INSERT INTO workflow_runs (run_id, name, session_id, status, model_key, "
                "budget_usd, resumed_from, started_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    child_id,
                    f"{parent.run_id}/{name}",
                    session_id,
                    "running",
                    model_key,
                    child_budget,
                    parent.run_id,
                    _now(),
                ),
            )
        child = WorkflowRun(
            child_id,
            loaded["script"],
            args=args,
            session_id=session_id,
            model_id=model_id,
            model_key=model_key,
            effort_label=effort_label,
            budget_usd=child_budget,
            depth=1,
        )
        outcome = await child.run()
        _finalize(child, outcome)
        parent.absorb_child(outcome)
        return outcome

    return nested


def _load_trusted_saved(name: str):
    from server.workflows import store

    loaded = store.load_script(name)
    return loaded if loaded and loaded["trusted"] else None


async def _drive(run: WorkflowRun) -> None:
    outcome = {"status": "failed", "error": "runner did not start"}
    try:
        outcome = await run.run()
    except Exception as e:  # noqa: BLE001
        log.error("workflow %s crashed: %s", run.run_id, e, exc_info=True)
        outcome = {"status": "failed", "error": str(e)}
    finally:
        _live.pop(run.run_id, None)
        _finalize(run, outcome)


def _finalize(run: WorkflowRun, outcome: dict) -> None:
    status = outcome.get("status", "failed")
    with _conn() as conn:
        _ensure_table(conn)
        conn.execute(
            "UPDATE workflow_runs SET status=?, agents_spawned=?, tokens_in=?, tokens_out=?, "
            "cost_usd=?, cap_reached=?, error=?, result_json=?, finished_at=? WHERE run_id=?",
            (
                status,
                outcome.get("agents_spawned", 0),
                outcome.get("tokens_in", 0),
                outcome.get("tokens_out", 0),
                outcome.get("cost_usd", 0.0),
                1 if outcome.get("cap_reached") else 0,
                outcome.get("error", ""),
                json.dumps(outcome.get("result")),
                _now(),
                run.run_id,
            ),
        )
    _publish(
        run.run_id,
        run.session_id,
        {
            "phase": "completed",
            "status": status,
            "agents_spawned": outcome.get("agents_spawned", 0),
            "cost_usd": outcome.get("cost_usd", 0.0),
            "cap_reached": bool(outcome.get("cap_reached")),
            "error": outcome.get("error", ""),
        },
    )


async def stop_run(run_id: str) -> bool:
    run = _live.get(run_id)
    if not run:
        return False
    await run.cancel()
    with _conn() as conn:
        _ensure_table(conn)
        conn.execute(
            "UPDATE workflow_runs SET status='stopped', finished_at=? WHERE run_id=?",
            (_now(), run_id),
        )
    _live.pop(run_id, None)
    return True


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("phases_json", "args_json", "result_json"):
        if k in d and d[k]:
            try:
                d[k.replace("_json", "")] = json.loads(d[k])
            except (ValueError, TypeError):
                d[k.replace("_json", "")] = None
        d.pop(k, None)
    d["cap_reached"] = bool(d.get("cap_reached"))
    d["live"] = row["run_id"] in _live
    return d


def get_run(run_id: str, *, journal_tail: int = 200) -> dict | None:
    with _conn() as conn:
        _ensure_table(conn)
        row = conn.execute("SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    out = _row_to_dict(row)
    out["journal"] = jnl.tail_journal(run_id, journal_tail)
    return out


def list_runs(session_id: str | None = None, *, limit: int = 50) -> list[dict]:
    with _conn() as conn:
        _ensure_table(conn)
        if session_id:
            rows = conn.execute(
                "SELECT * FROM workflow_runs WHERE session_id=? ORDER BY started_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM workflow_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def reconcile_stale() -> int:
    """Flip orphaned 'running' rows to 'stale' on boot (nothing is live yet)."""
    with _conn() as conn:
        _ensure_table(conn)
        cur = conn.execute(
            "UPDATE workflow_runs SET status='stale', finished_at=? "
            "WHERE status='running' AND run_id NOT IN (%s)" % (",".join("?" * len(_live)) or "''"),
            (_now(), *_live.keys()),
        )
        return cur.rowcount
