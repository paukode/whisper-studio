"""
Unattended run loop for scheduled cron jobs.

A run is an asyncio coroutine (scheduled as a tracked task by
server.cron_scheduler._spawn_cron_run) over the shared turn engine that chat,
subagents and headless runs use (server.chat.engine.runner.run_turn). It
mirrors server/agents/runtime.py's ``_run_agent_loop`` and
server/exec/headless.py's ``run_headless_turn``; read those first.

  - Tools, hooks, approvals and cost accounting go through the shared
    pipeline (run_turn -> server.tool_executor.execute_tool_batch /
    process_tool_results).
  - ``TurnContext.unattended=True``: a ``[WS_APPROVAL]`` request resolves
    under the unattended contract instead of waiting for a human, and
    ``_assemble_cron_tools`` drops ask_user_question.
  - Stop is ``asyncio.Task.cancel()`` (see server/cron_events.py).
  - Anthropic and OpenAI-on-Bedrock models both run; on-device (``local:*``)
    models are refused, the same as run_headless_turn.
  - The whole run, verify continuations included, gets the round limit and
    the time limit from Settings (server/infrastructure/run_limits.py).
  - After a natural finish the run is verified against its prompt
    (server.goals.cron_verify.verify, at most MAX_CONTINUATIONS
    continuations), then its result lands in cron_runs, the owning session
    or the cron inbox, and the cron_progress SSE frames.
"""

import asyncio
import functools
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace as _dc_replace
from datetime import UTC, datetime

log = logging.getLogger("whisper-studio")

# The feature name every Local-mode refusal of a scheduled run shows the user.
SCHEDULED_RUNS = "Scheduled runs"

# Dedicated pool for cron's blocking calls — the adapter's Bedrock invoke and
# the synchronous completion-verifier (server.goals.cron_verify.verify).
# Mirrors server.agents.runtime._agent_executor: its own pool so a burst of
# cron fires never head-of-line-blocks (or is blocked by) chat's per-
# connection executor or the agent runtime's.
_CRON_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cron-exec")


def _assemble_cron_tools(pool: list[dict]) -> list[dict]:
    """Tool list for an unattended cron turn.

    ask_user_question is dropped: it pauses for a human reply that never comes
    in an unattended run and would hang the job forever. Everything else passes
    through unchanged.
    """
    from server.tool_inputs import ToolCatalog

    available = pool.available if isinstance(pool, ToolCatalog) else pool
    return ToolCatalog(
        [t for t in pool if t["name"] != "ask_user_question"],
        available=[t for t in available if t["name"] != "ask_user_question"],
    )


def _merge_notifications(notifications: list[str], tail: str) -> str:
    """Fold captured notify_user messages into a pushed result.

    All exit paths route their text through here so a report delivered via
    notify_user (a side-effect, not tool output) is never dropped. Delivered
    blocks come first, then ``tail`` — the model's final text on success, or
    the status marker on failure. Empties and exact dupes are dropped (a
    model often both notifies AND echoes the same report).
    """
    blocks: list[str] = []
    seen: set[str] = set()
    for part in [*notifications, tail]:
        part = (part or "").strip()
        if part and part not in seen:
            seen.add(part)
            blocks.append(part)
    return "\n\n".join(blocks) if blocks else "(no output)"


async def _execute_cron_prompt(job_id: str) -> None:
    """Execute a cron job's prompt through the shared turn engine.

    Re-reads the job from disk to get the latest session_id/prompt. Runs as
    an asyncio task (see server.cron_scheduler._spawn_cron_run) — cancelling
    this task (server.cron_events.request_stop) is how a user-requested stop
    works.
    """
    # cron_scheduler owns load_cron_jobs, _push_result, the in-progress state,
    # and _server_loop. Import it lazily (a module-top import would be a cycle:
    # cron_scheduler imports this module).
    from server import cron_scheduler as _cs

    jobs = _cs.load_cron_jobs()
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if not job:
        log.error("Cron execute: job %s not found", job_id)
        return

    from server import cron_events

    with _cs._IN_PROGRESS_LOCK:
        if job_id in _cs._in_progress:
            log.info("Cron '%s' already running; skipping duplicate", job.get("name"))
            return
        _cs._in_progress.add(job_id)
        # Register the stop flag AND this task at the same instant the job is
        # marked in-progress: a stop request in the setup window (config/
        # tool-pool assembly) must not be silently lost. asyncio.current_task()
        # is this coroutine's own task, whichever way it was scheduled.
        cron_events.open_run(job_id, asyncio.current_task())

    run_id = str(uuid.uuid4())[:8]
    started = datetime.now(UTC)
    session_id = job.get("session_id", "")

    try:
        from server import cron_history

        cron_history.start_run(run_id, job_id, job.get("name", ""), session_id)
    except Exception as exc:
        log.warning("cron: failed to open run lease: %s", exc)

    def _elapsed_ms() -> int:
        return int((datetime.now(UTC) - started).total_seconds() * 1000)

    # Captured notify_user messages, folded into whichever exit path fires.
    # Bound before the try so the exception handler can always read it, even
    # on an early failure.
    notifications: list[str] = []
    try:
        from server.chat.tool_activation import activate_from_history
        from server.chat.tool_index import build_deferred_index
        from server.chat.tool_pool import assemble_partitioned_pool
        from server.infrastructure.config import load_config
        from server.infrastructure.sessions import visible_chat_history
        from server.local.runtime import is_local_model_id
        from server.workspace import get_workspace_path

        config = load_config()
        # Scheduled runs execute on a cloud model (on-device ids are refused
        # below), so Local mode refuses them before any model is resolved. The
        # refusal lands in the run history and the session like any failure;
        # the job itself is kept, so it runs again after a switch back.
        from server.infrastructure.cloud_guard import require_cloud

        require_cloud(SCHEDULED_RUNS, config)
        chat_models = config.get("chat_models", {})
        # Per-job model override: an explicit `model` key on the job wins when
        # it resolves to an Anthropic Bedrock id; otherwise the haiku-first
        # default chain.
        model_key = ""
        model_id = ""
        job_model = (job.get("model") or "").strip()
        if job_model and "anthropic" in str(chat_models.get(job_model, "")).lower():
            model_key = job_model
            model_id = chat_models[job_model]
        else:
            if job_model:
                log.warning(
                    "cron '%s': model %r unavailable or not Anthropic; using default",
                    job.get("name"),
                    job_model,
                )
            for candidate in ("haiku", "sonnet5.5"):
                if chat_models.get(candidate):
                    model_key = candidate
                    model_id = chat_models[candidate]
                    break
            else:
                if not chat_models:
                    raise RuntimeError(
                        "no chat models configured: chat_models is empty "
                        "(check config.user.json / config.example.json)"
                    )
                model_key, model_id = next(iter(chat_models.items()))

        if is_local_model_id(model_id):
            # On-device models need their own cold-start (llama_server.
            # ensure_serving) and capability probing (supports_tools, the
            # thinking levels), which scheduled runs do not wire. Matches
            # run_headless_turn's refusal (server/exec/headless.py).
            # _validate_job_model already keeps an explicit override from
            # reaching here (a local id never contains "anthropic"); this is
            # the defensive backstop for the haiku/sonnet/any-configured
            # fallback chain, which doesn't otherwise check.
            raise RuntimeError(
                f"model {model_id!r} is on-device (local:*), which scheduled "
                "runs cannot use yet — it needs its own cold-start/capability "
                "wiring (see run_headless_turn's identical restriction)"
            )

        # The round limit and time limit from Settings cover the whole run:
        # each verify continuation gets what is left of them, not a fresh set.
        from server.chat.engine.runner import SOFT_LIMIT_FRACTION
        from server.infrastructure.run_limits import round_limit, time_limit_seconds

        max_rounds = round_limit()
        time_limit = time_limit_seconds()
        run_started = time.monotonic()

        def _time_up() -> bool:
            # The engine forces the last round at the soft limit; past it a
            # continuation would get a sliver of time and end at once.
            return time.monotonic() - run_started >= SOFT_LIMIT_FRACTION * time_limit

        # The turn's only user message, built here (rather than down by
        # ctx = TurnContext(...)) so it's already in scope for
        # activate_from_history below; reused verbatim for the TurnContext.
        messages = [{"role": "user", "content": job["prompt"]}]

        # Progressive tool disclosure: re-derive this session's activations
        # from visible history first (self-healing across restarts, exactly
        # like server/chat/routes.py), then assemble the core+activated pool,
        # rebuilt every round like a chat turn's so a tool the job loads with
        # tool_search is offered from its next round, as the index promises.
        # Everything else is deferred into a compact index folded into the
        # system prompt below.
        activate_from_history(session_id, visible_chat_history(messages))
        # The run's tools work under this latch, like a chat turn's: once the
        # user disconnects (or switches away from) the workspace it started
        # in, its workspace tools are refused instead of acting on the folder
        # connected in its place, with nobody there to see it happen.
        from server.workspace.state import latch_workspace

        ws_latch = latch_workspace(get_workspace_path)
        _advertised, deferred, _core_count = assemble_partitioned_pool(
            session_id=session_id, chat=False
        )
        deferred_tool_index = build_deferred_index(deferred)

        def _tool_catalog() -> tuple[list[dict], int | None]:
            pool, _deferred, _core = assemble_partitioned_pool(session_id=session_id, chat=False)
            return _assemble_cron_tools(pool), None

        from server.prompts.rules import append_rules

        system = append_rules(
            "You are running an unattended scheduled task. There is no user to "
            "prompt, so act autonomously and never wait on input. Complete the "
            "task exactly as its instructions specify and return the finished "
            "result as your final message, in full. If the task asks for a "
            "report, briefing, or list, output that content itself - including "
            "every source link or citation the task requests - rather than a "
            "summary of the steps you took. You have the full tool set an "
            "interactive chat has: a cloud browser (Amazon Bedrock AgentCore "
            "MCP, when enabled) for web research, AWS reads via aws_boto3, and "
            "workspace tools when a folder is open. Use whatever the task needs."
        )
        if deferred_tool_index:
            system += "\n\n" + deferred_tool_index

        from server.infrastructure.feature_flags import is_enabled as _ff_enabled

        _caching_on = _ff_enabled("prompt_caching")

        cron_events.emit_progress(
            session_id,
            run_id=run_id,
            job_name=job.get("name", ""),
            phase="started",
            task=job.get("prompt", "")[:200],
            model=model_key,
            max_turns=max_rounds,
        )

        loop = asyncio.get_running_loop()

        # Provider adapter — the same chat_model_meta.provider check
        # server/chat/routes.py and agents/runtime.py both use.
        from server.chat.infra import _get_chat_model_meta, effort_for_model

        _provider = _get_chat_model_meta().get(model_key, {}).get("provider", "anthropic")
        # A scheduled run reasons at the app's configured effort, clamped to the
        # job's model. It used to pass None, which is not "the default" — it is
        # no ``thinking`` block at all, so every cron turn (and every agent it
        # spawned, which inherits this via ctx) ran shallower than the same work
        # done in chat.
        _effort_label = effort_for_model(model_key)
        if _provider == "openai_bedrock":
            from server.chat.engine.openai import OpenAIResponsesAdapter

            adapter = OpenAIResponsesAdapter(
                model_key=model_key,
                model_id=model_id,
                system_prompt=system,
                effort_label=_effort_label,
                session_id=session_id,
            )
        else:
            from server.chat.caching import CACHE_TTL
            from server.chat.engine.anthropic import AnthropicAdapter

            adapter = AnthropicAdapter(
                model_key=model_key,
                model_id=model_id,
                system_prompt=system,
                # The whole system prompt is the "static" cacheable block —
                # cron has no per-turn dynamic RAG portion the way chat does.
                system_static=system,
                system_dynamic="",
                caching_on=_caching_on,
                cache_ttl=CACHE_TTL,
                effort_label=_effort_label,
                loop=loop,
                executor=_CRON_EXECUTOR,
            )

        from server.chat.engine.policy import TurnPolicy
        from server.chat.engine.runner import TurnContext, run_turn
        from server.goals.cron_verify import MAX_CONTINUATIONS
        from server.goals.cron_verify import verify as _cron_verify

        ctx = TurnContext(
            session_id=session_id,
            model_key=model_key,
            model_id=model_id,
            messages=messages,
            adapter=adapter,
            policy=TurnPolicy(
                max_rounds=max_rounds,
                deadline_seconds=time_limit,
                # Cron keeps its OWN verify-and-continue below, called as an
                # explicit step after each run_turn call — never the engine's
                # automatic completion_gate (Stop hooks + goal evaluator).
                # Exactly like agents: an unattended run never runs Stop hooks.
                completion_gate=False,
            ),
            loop=loop,
            executor=_CRON_EXECUTOR,
            cost_source="cron",
            tool_exec_model_id=model_id,
            # Agents this run spawns inherit the same level (and the ultracode
            # directive with it, when that is what the app is configured to).
            effort_label=_effort_label,
            # Cron never ran interactive chat's post-turn memory hooks (auto
            # memory / session memory / dream consolidation) before either.
            memory_hooks=lambda msgs: None,
            unattended=True,
            turn_scope_id=f"cron:{job_id}",
            tool_catalog=_tool_catalog,
            ws_latch=ws_latch,
        )

        # ── Drain run_turn, possibly more than once (verify-and-continue
        # calls it again over the accumulated messages when the prompt isn't
        # satisfied yet) ── THIS PARSING IS COUPLED TO runner.py's PINNED SSE
        # FRAME VOCABULARY, exactly like _run_agent_loop's/run_headless_turn's
        # own drains — see runner.py's module docstring before touching this.
        turn_no = 0  # rounds consumed so far, across every run_turn() call
        final_text_parts: list[str] = []
        round_text_parts: list[str] = []
        # The reply the last run_turn() call finished on. The runner keeps
        # every round it continues from (a tool round, a cut-off answer) in
        # ctx.messages, but not that final one, so the verifier and a verify
        # continuation get it from here: the text of the last round, unless
        # that round called tools.
        final_reply = ""
        round_called_tools = False
        had_error: str | None = None
        verify_continuations = 0
        verdict = None

        def _flush_round_text() -> None:
            if not round_text_parts:
                return
            text = "".join(round_text_parts)
            round_text_parts.clear()
            if text.strip():
                final_text_parts.append(text)

        while True:
            remaining = max_rounds - turn_no
            if remaining <= 0 or _time_up():
                break
            # Each continuation gets what is LEFT of the run's rounds and time,
            # not a fresh budget.
            ctx.policy = _dc_replace(
                ctx.policy,
                max_rounds=remaining,
                deadline_seconds=time_limit - (time.monotonic() - run_started),
            )

            final_reply = ""
            async for chunk in run_turn(ctx):
                for raw_line in chunk.splitlines():
                    if not raw_line.startswith("data: "):
                        continue  # heartbeat comments (": hb") and blank lines
                    payload = raw_line[len("data: ") :]
                    if payload == "[DONE]":
                        continue
                    try:
                        frame = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    if "usage" in frame:
                        # A round's calls stream before its usage frame.
                        turn_no += 1
                        final_reply = "" if round_called_tools else "".join(round_text_parts)
                        round_called_tools = False
                        _flush_round_text()
                        cron_events.emit_progress(
                            session_id,
                            run_id=run_id,
                            job_name=job.get("name", ""),
                            phase="turn_start",
                            turn=turn_no + 1,
                            max_turns=max_rounds,
                        )
                    elif "text" in frame and isinstance(frame.get("text"), str):
                        round_text_parts.append(frame["text"])
                    elif "error" in frame:
                        had_error = frame["error"]
                    elif "skill_input" in frame:
                        round_called_tools = True
                        tool_name = frame["skill_input"]
                        cron_events.emit_progress(
                            session_id,
                            run_id=run_id,
                            job_name=job.get("name", ""),
                            phase="tool_call",
                            turn=turn_no + 1,
                            tool_name=tool_name,
                            tool_input_preview=str(frame.get("input") or {})[:200],
                        )
                    elif "skill_result" in frame:
                        tool_name = frame["skill_result"]
                        output_preview = frame.get("output") or ""
                        cron_events.emit_progress(
                            session_id,
                            run_id=run_id,
                            job_name=job.get("name", ""),
                            phase="tool_result",
                            turn=turn_no,
                            tool_name=tool_name,
                            output_preview=output_preview[:200],
                        )
                    elif "notify_user" in frame:
                        msg = (frame["notify_user"] or {}).get("message", "")
                        if msg:
                            notifications.append(msg)
            _flush_round_text()

            if had_error is not None:
                # A terminal round-level error: skip verify entirely and fall
                # through to the failure exit below.
                break

            if not _ff_enabled("cron_verify"):
                break

            # Verify the run actually satisfied its prompt before
            # reporting success — on EVERY natural finish (a run finishing on
            # its last allowed round is verified too; only the continuation
            # is round-gated). Synchronous and safe to block a worker thread
            # on (never the event loop) — same reasoning as
            # server.goals.gate.run_completion_gate's own evaluator call.
            # The verifier reads the history WITH the final reply, in a new
            # list (as the chat gate's _gated_messages does): the runner's
            # list lacks it, and a task that answers in text was judged as
            # having produced nothing.
            reply = [{"role": "assistant", "content": final_reply}] if final_reply.strip() else []
            verdict = await loop.run_in_executor(
                _CRON_EXECUTOR,
                functools.partial(
                    _cron_verify,
                    job["prompt"],
                    [*ctx.messages, *reply],
                    notifications,
                    main_model_key=model_key,
                    session_id=session_id,
                ),
            )
            if (
                verdict.is_achieved
                # A verifier that could not judge spends no continuation: the
                # run is reported unverified with the reason below.
                or verdict.is_not_checked
                or verify_continuations >= MAX_CONTINUATIONS
                or turn_no >= max_rounds
                or _time_up()
            ):
                break
            verify_continuations += 1
            # The continuation answers the feedback with its own last reply
            # in view, the way a chat gate block keeps it.
            ctx.messages.extend(reply)
            ctx.messages.append(
                {
                    "role": "user",
                    "content": (
                        f"[verify] {verdict.feedback} The task is not yet "
                        "complete. Keep working until it is genuinely done, "
                        "then output the finished result."
                    ),
                }
            )
            # Loop again: another run_turn() call over the same accumulated
            # ctx.messages, with the reduced remaining-round budget.

        if had_error is not None:
            cron_events.emit_progress(
                session_id,
                run_id=run_id,
                job_name=job.get("name", ""),
                phase="failed",
                turn=turn_no,
            )
            _cs._push_result(
                job,
                _merge_notifications(notifications, f"[FAILED] {had_error}"),
                status="failed",
                run_id=run_id,
                duration_ms=_elapsed_ms(),
            )
            return

        collected_text = "\n\n".join(final_text_parts)
        result_text = _merge_notifications(notifications, collected_text)
        status = "ok"
        if verdict is not None and not verdict.is_achieved:
            unchecked = "Not checked: " if verdict.is_not_checked else ""
            result_text = f"[UNVERIFIED] {unchecked}{verdict.feedback}\n\n{result_text}"
            status = "failed"
        cron_events.emit_progress(
            session_id,
            run_id=run_id,
            job_name=job.get("name", ""),
            phase="completed",
            turn=turn_no,
        )
        _cs._push_result(job, result_text, status=status, run_id=run_id, duration_ms=_elapsed_ms())
        log.info(
            "Cron '%s' result pushed (session=%s, status=%s)",
            job.get("name"),
            session_id,
            status,
        )

    except asyncio.CancelledError:
        # User-requested stop (server.cron_events.request_stop cancels this
        # task). Report cleanly, then re-raise so the task ends genuinely cancelled (mirrors
        # server.agents.runtime.run_agent's own CancelledError handling).
        cron_events.emit_progress(
            session_id,
            run_id=run_id,
            job_name=job.get("name", ""),
            phase="stopped",
        )
        _cs._push_result(
            job,
            _merge_notifications(notifications, "[stopped] run stopped by user"),
            status="stopped",
            run_id=run_id,
            duration_ms=_elapsed_ms(),
        )
        raise
    except Exception as e:
        log.error("Cron execution failed for '%s': %s", job.get("name"), e)
        cron_events.emit_progress(
            session_id,
            run_id=run_id,
            job_name=job.get("name", ""),
            phase="failed",
        )
        _cs._push_result(
            job,
            _merge_notifications(notifications, f"[FAILED] {e}"),
            status="failed",
            run_id=run_id,
            duration_ms=_elapsed_ms(),
        )
    finally:
        cron_events.close_run(job_id)
        with _cs._IN_PROGRESS_LOCK:
            _cs._in_progress.discard(job_id)
