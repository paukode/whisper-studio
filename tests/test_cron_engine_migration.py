"""P4: cron ported onto the shared agentic turn engine
(server.chat.engine.runner.run_turn), same as agents/runtime.py and
server/exec/headless.py before it.

Covers the migration-specific guarantees that didn't have a home in the
adapted test_cron_tools.py / test_goal_cron_verify.py:

  - hooks (PreToolUse) now fire NATIVELY for a cron tool call (no thread
    bridge — the old _cron_pre_hook is gone entirely)
  - completion_gate stays False: a global Stop hook is never consulted
    during a cron run, and cron's OWN verify-and-continue still caps at
    MAX_CONTINUATIONS=2, not the goal-gate's (much larger) default cap
  - the old hand-rolled loop and its thread-bridge helper are genuinely
    gone, not renamed-and-unused or reachable via a fallback
  - stop responsiveness under asyncio.Task.cancel() is comparable to (in
    practice much faster than) the old 1-second poll
  - cron_history's start_run/finish_run lease lifecycle still works
    correctly now that a run is an asyncio task instead of a thread
  - the boot-time downtime catch-up fire actually runs a job instead of
    silently building an unawaited coroutine (a real bug this migration
    would otherwise have introduced: _run became a coroutine function, and
    its old call site only built the coroutine object)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import server.cron_scheduler as C

sys.path.insert(0, os.path.dirname(__file__))
from test_cron_tools import _run_cron_job  # noqa: E402
from test_goal_cron_verify import _run as _run_with_verdicts  # noqa: E402

from tests.golden_harness import (  # noqa: E402
    FakeBedrockClient,
    msg_end,
    msg_start,
    text_block,
    tool_use_block,
)


def _job(**overrides) -> dict:
    job = {
        "id": "job-migration",
        "name": "migration-job",
        "prompt": "do the thing",
        "session_id": "sess-migration",
        "schedule": {"type": "interval", "seconds": 1800},
        "enabled": True,
    }
    job.update(overrides)
    return job


# ── Hooks fire natively (no thread bridge) ───────────────────────────────────


def test_pretooluse_hook_blocks_a_cron_tool_call(monkeypatch):
    """PreToolUse now runs via the SAME native `await run_hooks(...)` every
    other unattended/interactive caller uses (server/tool_executor.py) — no
    _cron_pre_hook thread-bridge is involved. A denying in-process hook must
    actually block the call and the denial reason must reach the model."""
    from server.infrastructure import plugin_hooks

    saved = dict(plugin_hooks._hooks)
    plugin_hooks._hooks.clear()
    try:

        async def deny(payload):
            if payload.get("tool_name") == "web_search":
                return {"decision": "deny", "reason": "no web access this run"}
            return None

        plugin_hooks.register_hook("PreToolUse", deny)

        fake_stream = FakeBedrockClient(
            [
                [
                    msg_start(),
                    *tool_use_block("tu_1", "web_search", {"query": "x"}),
                    *msg_end(stop_reason="tool_use"),
                ],
                [msg_start(), *text_block("done"), *msg_end(stop_reason="end_turn")],
            ]
        )

        async def fake_route_tool(name, tool_input, **kwargs):
            raise AssertionError("route_tool must not run for a hook-denied call")

        recorded: dict = {}
        _run_cron_job(monkeypatch, _job(), fake_stream, fake_route_tool, recorded)

        assert recorded.get("status") == "ok"
        # The denial reason reached the model as the tool_result on round 2.
        assert "no web access this run" in json.dumps(fake_stream.requests[1])
    finally:
        plugin_hooks._hooks.clear()
        plugin_hooks._hooks.update(saved)


# ── completion_gate stays False; cron's own cap governs, not the gate's ─────


def test_stop_hooks_never_consulted_during_a_cron_run(monkeypatch):
    """Cron sets TurnPolicy(completion_gate=False), exactly like agents — a
    globally-registered Stop hook must never fire for a cron run. If it did,
    that would be NEW, unrequested Stop-hook exposure for unattended
    automation introduced by this migration."""
    from server.infrastructure import plugin_hooks

    saved = dict(plugin_hooks._hooks)
    plugin_hooks._hooks.clear()
    calls = {"stop": 0}
    try:

        async def always_block(payload):
            if payload.get("event") == "Stop":
                calls["stop"] += 1
                return {"decision": "deny", "reason": "never allowed to stop"}
            return None

        plugin_hooks.register_hook("Stop", always_block)

        fake_stream = FakeBedrockClient(
            [[msg_start(), *text_block("done"), *msg_end(stop_reason="end_turn")]]
        )

        async def fake_route_tool(name, tool_input, **kwargs):
            return ("ok", [])

        recorded: dict = {}
        _run_cron_job(monkeypatch, _job(), fake_stream, fake_route_tool, recorded)

        # The run finished normally on its first natural end_turn — an
        # always-blocking Stop hook, if consulted, would have forced it to
        # keep going (or hit a cap frame) instead.
        assert recorded.get("status") == "ok"
        assert calls["stop"] == 0
    finally:
        plugin_hooks._hooks.clear()
        plugin_hooks._hooks.update(saved)


def test_verify_continuation_cap_is_two_not_the_goal_gates_default(monkeypatch):
    """cron's own verify-and-continue (server.goals.cron_verify,
    MAX_CONTINUATIONS=2) governs how many times an unmet run extends — NOT
    server.goals.DEFAULT_MAX_CONSECUTIVE_BLOCKS (8), the completion gate's
    own cap. Since completion_gate=False, the gate's cap never applies here
    at all; this pins the actually-enforced number."""
    from server.goals import DEFAULT_MAX_CONSECUTIVE_BLOCKS, Verdict
    from server.goals.cron_verify import MAX_CONTINUATIONS

    assert MAX_CONTINUATIONS == 2
    assert DEFAULT_MAX_CONSECUTIVE_BLOCKS == 8  # sanity: they really do differ

    recorded, calls = _run_with_verdicts(
        monkeypatch,
        [Verdict("not_achieved", "still missing", 0.5)],
        max_rounds_calls=MAX_CONTINUATIONS + 2,
    )
    # Exactly the initial check plus MAX_CONTINUATIONS extensions — never
    # anywhere near the goal-gate's own (much larger) default cap.
    assert calls["n"] == MAX_CONTINUATIONS + 1
    assert recorded["status"] == "failed"


# ── Full retirement: the old loop and its thread-bridge are actually gone ───


def test_old_hand_rolled_loop_is_fully_retired():
    """_cron_pre_hook (the PreToolUse thread-bridge) and _finish_stopped (the
    old thread's cooperative-stop exit helper) must not exist anywhere —
    renamed-and-unused, commented out, or behind a fallback all count as
    still present and are exactly what "full retirement" rules out."""
    import pathlib
    import subprocess

    import server.cron_run as CR

    assert not hasattr(CR, "_cron_pre_hook")
    assert not hasattr(CR, "_finish_stopped")

    # Call-syntax search (not a bare substring): a prose mention explaining
    # what changed (like this test's own docstring, or cron_run.py's module
    # docstring) must not trip this — an actual call or def is what matters.
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    this_file = pathlib.Path(__file__).name
    for name in ("_cron_pre_hook", "_finish_stopped"):
        out = subprocess.run(
            ["grep", "-rn", "--include=*.py", f"{name}(", "server", "tests"],
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        hits = [line for line in out.stdout.splitlines() if this_file not in line]
        assert hits == [], f"{name!r} still called/defined somewhere: {hits}"


# ── Stop responsiveness under asyncio.Task.cancel() ─────────────────────────


def test_stop_cancels_an_in_flight_cron_run_quickly(monkeypatch):
    """A stop request must interrupt a running cron job at speed comparable
    to (in practice faster than) the old thread's 1-second poll slices —
    now via asyncio.Task.cancel() (server.cron_events.request_stop)."""
    import server.chat.tool_pool as TP
    import server.infrastructure.config as CFG
    import server.prompts.rules as R
    import server.tool_executor as TE
    import server.workspace as WS
    from server import cron_events

    job = _job(id="job-stop-speed", name="stop-speed")

    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("tu_1", "slow_tool", {}),
                *msg_end(stop_reason="tool_use"),
            ],
        ]
    )

    started = asyncio.Event()

    async def fake_route_tool(name, tool_input, **kwargs):
        started.set()
        # Long enough that the test would time out if cancellation didn't
        # actually interrupt it — asyncio.sleep is natively cancellable.
        await asyncio.sleep(30)
        return ("finished", [])  # pragma: no cover — never reached if stop works

    monkeypatch.setattr(C, "load_cron_jobs", lambda: [job])
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    monkeypatch.setattr(TP, "assemble_tool_pool", lambda **k: [])
    monkeypatch.setattr(WS, "get_workspace_path", lambda: "")
    monkeypatch.setattr(R, "append_rules", lambda s: s)
    monkeypatch.setattr(TE, "route_tool", fake_route_tool)
    monkeypatch.setattr(
        CFG,
        "load_config",
        lambda: {
            "chat_models": {"haiku": "fake-model-id"},
            "cron_max_runs_per_job": 200,
            "feature_flags": {"cron_verify": False},
        },
    )

    recorded: dict = {}

    def fake_push(job, text, status="ok", *, run_id, duration_ms=None):
        recorded["text"] = text
        recorded["status"] = status

    monkeypatch.setattr(C, "_push_result", fake_push)

    import server.cron_history as H

    monkeypatch.setattr(H, "start_run", lambda *a, **k: None)

    async def _drive() -> float:
        task = asyncio.ensure_future(C._execute_cron_prompt(job["id"]))
        await asyncio.wait_for(started.wait(), timeout=2.0)
        t0 = time.monotonic()
        assert cron_events.request_stop(job["id"]) is True
        # The task re-raises CancelledError after its own cleanup (mirrors
        # server.agents.runtime.run_agent) — awaiting it propagates that,
        # which is the expected, successful outcome here.
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.CancelledError:
            pass
        return time.monotonic() - t0

    elapsed = asyncio.run(_drive())
    assert elapsed < 1.0
    assert recorded.get("status") == "stopped"


# ── cron_history lease lifecycle across the new asyncio-task execution ─────


def test_cron_history_lease_transitions_running_to_terminal(monkeypatch, tmp_path):
    """The run's lifecycle changed from thread to asyncio task — cron_history
    (start_run at the top, finish_run via _push_result at the end) must still
    see it correctly: a 'running' lease is opened, and a clean finish lands a
    terminal status that a boot-time reconcile does NOT then flag as
    interrupted. (The reconciler's own correctness against a genuinely
    orphaned lease is covered separately, execution-model-agnostically, by
    test_reconcile_all_running_at_boot_fails_fresh_lease in
    test_cron_schedule.py.)"""
    import server.chat.tool_pool as TP
    import server.cron_history as H
    import server.infrastructure.config as CFG
    import server.prompts.rules as R
    import server.workspace as WS

    monkeypatch.setattr(H, "DB_PATH", str(tmp_path / "hist.db"))

    job = _job(id="job-hist", name="hist-job", session_id="sess-hist")

    fake_stream = FakeBedrockClient(
        [[msg_start(), *text_block("done"), *msg_end(stop_reason="end_turn")]]
    )

    monkeypatch.setattr(C, "load_cron_jobs", lambda: [job])
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    monkeypatch.setattr(TP, "assemble_tool_pool", lambda **k: [])
    monkeypatch.setattr(WS, "get_workspace_path", lambda: "")
    monkeypatch.setattr(R, "append_rules", lambda s: s)
    monkeypatch.setattr(
        CFG,
        "load_config",
        lambda: {
            "chat_models": {"haiku": "fake-model-id"},
            "cron_max_runs_per_job": 200,
            "feature_flags": {"cron_verify": False},
        },
    )
    # _push_result's OTHER side effect (delivering into a chat session) is
    # unrelated to the history-lease lifecycle this test cares about.
    monkeypatch.setattr(C, "_resolve_target_session", lambda sid: "")
    monkeypatch.setattr(C, "_emit_cron_event", lambda *a, **k: None)

    # cron_history.start_run/finish_run are the REAL functions here (not
    # stubbed) so the lease's actual DB state can be inspected.
    asyncio.run(C._execute_cron_prompt(job["id"]))

    runs = H.list_runs("job-hist")
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["finished_at"] is not None

    # A boot-time reconcile after a clean finish must not touch it.
    assert H.reconcile_stale(all_running=True) == 0


# ── Boot-time downtime catch-up actually fires (not a dropped coroutine) ────


def test_boot_catchup_fire_actually_runs_the_job(monkeypatch):
    """_make_runner's _run became a coroutine function as part of this
    migration (so APScheduler schedules it directly on the loop instead of
    its thread-pool fallback). _add_job_to_scheduler's downtime catch-up
    branch used to call it as `_make_runner(job["id"])()` — for an async
    function that only builds the coroutine object, it does not run it. Left
    unfixed, every downtime-missed catch-up fire would have silently stopped
    firing (just a "coroutine was never awaited" warning in the logs, no
    error anyone would notice). This proves the fix: the catch-up branch
    actually starts the run.
    """
    called: dict = {}

    async def fake_execute(job_id):
        called["job_id"] = job_id

    job = _job(id="job-catchup", name="catchup-job", last_run="2000-01-01T00:00:00Z")

    monkeypatch.setattr(C, "_execute_cron_prompt", fake_execute)
    monkeypatch.setattr(C, "_scheduler", object())  # truthy is all this path needs
    monkeypatch.setattr(C, "cron_catch_up_due", lambda *a, **k: True)
    # _run (built by _make_runner, which the catch-up branch calls) does its
    # own job lookup/marking via _mutate_jobs -> load_cron_jobs/save_cron_jobs
    # before it ever reaches _spawn_cron_run.
    monkeypatch.setattr(C, "load_cron_jobs", lambda: [dict(job)])
    monkeypatch.setattr(C, "save_cron_jobs", lambda jobs: None)

    async def _drive():
        # Normally captured once at init_scheduler() startup.
        monkeypatch.setattr(C, "_server_loop", asyncio.get_running_loop())
        C._add_job_to_scheduler(job, catch_up=True)
        # The catch-up fire is scheduled as a tracked background task
        # (server.infrastructure.async_tasks.spawn) whose own body awaits a
        # real thread (asyncio.to_thread) before reaching _execute_cron_prompt
        # — wait for it properly instead of guessing a tick count.
        from server.infrastructure.async_tasks import _TASKS

        for _ in range(200):
            pending = [t for t in _TASKS if not t.done()]
            if not pending:
                break
            await asyncio.sleep(0.01)

    asyncio.run(_drive())
    assert called.get("job_id") == "job-catchup"
