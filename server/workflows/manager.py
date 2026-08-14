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
# run_id -> background-task registry id for the same run. In-memory like _live
# (a restart kills the run anyway, and boot reconcile flips both sides), so a
# missing entry just means "nothing to close".
_task_ids: dict[str, str] = {}

# Workflow status (this module's vocabulary) -> registry status + its event.
_TASK_TERMINAL = {
    "done": ("completed", "task_completed"),
    "completed": ("completed", "task_completed"),
    "stopped": ("stopped", "task_stopped"),
}


def _register_task(run_id: str, name: str, session_id: str, model_key: str) -> str:
    """Mirror a launched run into the unified background-task registry.

    The registry has allowed ``kind="workflow"`` since it was written and
    completion_inject already reports finished workflow tasks back to the model
    on the next turn — but nothing ever created such a row, so both were dead
    code and a detached run notified nobody: no Background Tasks entry, no chat
    card, no next-turn report. Best-effort by design; a registry hiccup must
    never stop the run itself.
    """
    try:
        from server.tasks import registry
        from server.tasks.events import emit_task_event

        task_id = registry.create_task(
            "workflow",
            session_id=session_id,
            title=f"Workflow: {name or run_id}",
            meta={"run_id": run_id, "model": model_key},
        )
        row = registry.get_task(task_id)
        if row and session_id:
            emit_task_event(session_id, "task_started", row)
        return task_id
    except Exception as e:  # noqa: BLE001 — mirroring is never load-bearing
        log.warning("workflow %s: task registry mirror failed: %s", run_id, e)
        return ""


def _finish_task(run_id: str, status: str, outcome: dict) -> None:
    """Close the mirrored task so the run's end is announced once, wherever
    background tasks are shown, and picked up by completion_inject."""
    task_id = _task_ids.pop(run_id, "")
    if not task_id:
        return
    try:
        from server.tasks import registry
        from server.tasks.events import emit_task_event

        task_status, event = _TASK_TERMINAL.get(status, ("failed", "task_failed"))
        finished = registry.finish_task(
            task_id, status=task_status, result_text=_task_result_text(status, outcome)
        )
        if finished:
            emit_task_event(finished.get("session_id") or "", event, finished)
    except Exception as e:  # noqa: BLE001
        log.warning("workflow %s: task registry close failed: %s", run_id, e)


def _task_result_text(status: str, outcome: dict) -> str:
    """What the user's task card and the model's next-turn injection read.
    Leads with the headline numbers, then the script's own return value."""
    head = (
        f"Workflow {status}: {outcome.get('agents_spawned', 0)} agent(s), "
        f"${outcome.get('cost_usd', 0.0):.2f}, "
        f"{outcome.get('tokens_in', 0) + outcome.get('tokens_out', 0)} tokens"
        + (" (budget cap reached)" if outcome.get("cap_reached") else "")
    )
    if outcome.get("error"):
        return f"{head}\n\nError: {outcome['error']}"
    try:
        body = json.dumps(outcome.get("result"), indent=2)
    except (TypeError, ValueError):
        body = str(outcome.get("result"))
    if body and body != "null":
        return f"{head}\n\nResult:\n{body[:4000]}"
    return head


def resolve_workflow_effort(effort_label: str | None, model_key: str) -> str | None:
    """Clamp a run's requested effort to what its model actually supports.

    ``None`` in means "not supplied" — an omission, not a decision — so it falls
    back to the configured default rather than to no effort at all. That is what
    kept a manually approved run reasoning shallower than an auto-approved one.
    ``None`` out means the model has no effort support (Haiku).
    """
    try:
        from server.chat.infra import effort_for_model

        return effort_for_model(model_key, effort_label)
    except Exception:  # noqa: BLE001 — config unavailable; infer from the key
        from server.infrastructure.effort import resolve_effort

        return resolve_effort({}, model_key, effort_label)


def resolve_workflow_workspace(raw: str | None) -> tuple[str | None, str]:
    """Resolve the workspace a run is pinned to. Returns (path, error) — path
    is ``None`` with error set on a bad explicit path, or ``None`` with no
    error when nothing is connected and none was given (the fully-legacy,
    unpinned case — nothing to pin to).

    An explicit path (the model resolved one from the user's own words, e.g.
    "check ~/Downloads/project1") must already exist: silently creating it, or
    falling back to whatever else happens to be connected, is exactly the
    "workflow ran against the wrong repo" failure this exists to prevent — the
    model can call ws_open_folder first if the folder genuinely needs creating.
    Omitted, it snapshots whatever is connected RIGHT NOW, so a workspace
    switch after launch can never redirect an already-running run's later
    agents.
    """
    from server.workspace.state import get_workspace_path

    connected = get_workspace_path()
    raw = (raw or "").strip()
    if raw:
        resolved = os.path.realpath(os.path.expanduser(raw))
        if not os.path.isdir(resolved):
            return None, f"Error: workspace path '{raw}' does not exist or is not a directory."
        # Pinning a run to a folder the user has not approved would give every
        # agent in that run read/write reach into it without ever asking. The
        # grant is asked for once (ws_open_folder's approval card) and
        # remembered, so this only ever fires the first time.
        from server.security.folder_grants import needs_grant

        if needs_grant(resolved, connected):
            return None, (
                f"Error: '{raw}' is outside the connected workspace and has not been "
                "approved yet. Call ws_open_folder with that path first so the user "
                "can grant access (they are only asked once), then retry."
            )
        return resolved, ""
    return (os.path.realpath(connected), "") if connected else (None, "")


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
    workspace_path: str | None = None,
    budget_tokens: int | None = None,
    phases: list | None = None,
    name: str = "",
    resume_from: str = "",
    agent_runner=None,
) -> str:
    """Register + launch a run detached; returns its run_id immediately.

    ``workspace_path`` is taken as-is, already resolved by the caller — unlike
    effort, it must NOT be re-derived here: a value shown on an approval card
    has to be the exact value the run launches with, or the card would be
    lying about what it is asking the user to approve. Every entry point
    resolves it once, at the point a script is either previewed or launched
    (server.workflows.manager.resolve_workflow_workspace), and callers
    re-validate it still exists right before calling this rather than
    swapping to something else if it does not.
    """
    from server.infrastructure.async_tasks import spawn
    from server.workflows.runtime import DEFAULT_WORKFLOW_BUDGET_TOKENS

    # Every run is budgeted: entry points resolve the default early so the
    # approval card shows the real number, but enforce it here too so no
    # caller can launch uncapped by omission.
    if budget_tokens is None:
        budget_tokens = DEFAULT_WORKFLOW_BUDGET_TOKENS

    # The run's effort is the session's effort, resolved once against the run's
    # own model and then frozen for every agent it spawns. Resolving HERE means
    # every entry point (tool dispatch, approval card, saved-workflow launch,
    # resume) lands on the same value instead of each carrying its own default.
    effort_label = resolve_workflow_effort(effort_label, model_key)

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
            "args_json, model_key, effort_label, workspace_path, budget_tokens, resumed_from, "
            "started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                name,
                session_id,
                "running",
                json.dumps(phases or []),
                json.dumps(args),
                model_key,
                effort_label,
                workspace_path,
                budget_tokens,
                resume_from,
                _now(),
            ),
        )

    _task_ids[run_id] = _register_task(run_id, name, session_id, model_key)

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
        workspace_path=workspace_path,
        budget_tokens=budget_tokens,
        resume_cache=resume_cache,
        journal=journal,
        agent_runner=agent_runner,
        nested_runner=_make_nested_runner(
            session_id, model_key, model_id, effort_label, workspace_path
        ),
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


def _make_nested_runner(session_id, model_key, model_id, effort_label, workspace_path):
    async def nested(name, args, parent):
        loaded = _load_trusted_saved(name)
        if not loaded:
            return {
                "status": "failed",
                "error": (
                    f"unknown or untrusted workflow: {name} "
                    "(nested calls need the saved workflow trusted in Settings > Workflows)"
                ),
            }
        # Depth-1 child inherits the parent's REMAINING budget so nested spend
        # can't exceed the parent's cap; its usage is merged back into the parent.
        child_budget = None
        if parent.budget_tokens is not None:
            child_budget = max(0, parent.budget_tokens - parent.tokens_out)
        child_id = uuid.uuid4().hex[:12]
        # Give the nested run its own row so it is queryable and its journal dir
        # isn't an orphan; namespace the name under the parent.
        with _conn() as conn:
            _ensure_table(conn)
            conn.execute(
                "INSERT INTO workflow_runs (run_id, name, session_id, status, model_key, "
                "effort_label, workspace_path, budget_tokens, resumed_from, started_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    child_id,
                    f"{parent.run_id}/{name}",
                    session_id,
                    "running",
                    model_key,
                    effort_label,
                    workspace_path,
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
            # A nested workflow() call is not a new user instruction naming a
            # folder — it inherits the SAME pinned root as its parent.
            workspace_path=workspace_path,
            budget_tokens=child_budget,
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
    """Record a run's terminal state — FIRST writer wins.

    Stopping a run and the run finishing naturally are two independent
    writers racing for the same row: stop_run cancels the harness and writes
    'stopped', while _drive is still awaiting run() and will write whatever
    outcome that returns (usually 'failed', since the harness was just
    killed). Without a guard the later write clobbers the earlier one, so a
    run the user deliberately stopped shows up as failed, and the completion
    event fires twice.

    The ``WHERE status='running'`` clause makes the claim atomic in SQLite:
    exactly one writer's UPDATE touches a row, and only that one announces
    the run's end.
    """
    status = outcome.get("status", "failed")
    with _conn() as conn:
        _ensure_table(conn)
        cur = conn.execute(
            "UPDATE workflow_runs SET status=?, agents_spawned=?, tokens_in=?, tokens_out=?, "
            "cost_usd=?, cap_reached=?, error=?, result_json=?, finished_at=? "
            "WHERE run_id=? AND status='running'",
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
        claimed = cur.rowcount > 0
    if not claimed:
        # Someone else (stop_run, or a boot reconcile that flipped this to
        # 'stale') already recorded the end. Their status stands and their
        # event already went out; adding ours would contradict it.
        log.info("workflow %s already terminal; not overwriting with %s", run.run_id, status)
        return
    _finish_task(run.run_id, status, outcome)
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
    """Stop a live run. Claims the terminal state the same first-writer-wins
    way _finalize does, so a run that finished naturally in the instant
    between the click and this write keeps its real outcome instead of being
    relabelled 'stopped'."""
    run = _live.get(run_id)
    if not run:
        return False
    await run.cancel()
    with _conn() as conn:
        _ensure_table(conn)
        cur = conn.execute(
            "UPDATE workflow_runs SET status='stopped', finished_at=? "
            "WHERE run_id=? AND status='running'",
            (_now(), run_id),
        )
        claimed = cur.rowcount > 0
    if claimed:
        # Only the winner closes the mirrored background task, so the run's
        # end is announced exactly once.
        _finish_task(run_id, "stopped", {})
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
