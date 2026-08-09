"""
Unattended run loop for scheduled cron jobs.

Ports cron onto the exact shared agentic turn engine interactive chat,
subagents, and headless runs all use (server.chat.engine.runner.run_turn) —
mirrors server/agents/runtime.py's ``_run_agent_loop`` and
server/exec/headless.py's ``run_headless_turn`` almost exactly. Read those
first if you're touching this one.

What changed from the old hand-rolled ``boto3.invoke_model`` loop:

  - Runs as a native ``asyncio`` coroutine (scheduled as a tracked task by
    server.cron_scheduler._spawn_cron_run) instead of a ``threading.Thread``
    target. Tool execution, hooks (PreToolUse/PostToolUse/PostToolUseFailure),
    approval handling, and cost accounting all flow through the SAME shared
    pipeline chat/agents use (server.chat.engine.runner.run_turn ->
    server.tool_executor.execute_tool_batch/process_tool_results) instead of
    a cron-specific re-implementation — so ``_cron_pre_hook``'s thread-bridge
    is gone; ``run_hooks`` is awaited natively, same as every other caller.
  - ``TurnContext.unattended=True`` (mirroring agents/headless) resolves
    pause-inducing tools without a human: a ``[WS_APPROVAL]`` request now
    executes inline automatically (like a subagent), rather than the old
    behavior of failing the run with "[stopped] a tool requested user
    input...". This is a genuine, deliberate behavior change from unifying
    onto the shared engine's unattended contract — see the migration's
    final report for the full reasoning.
  - Stop is now ``asyncio.Task.cancel()`` (see server/cron_events.py) instead
    of a 1-second poll around each tool's bridged future.
  - Anthropic OR OpenAI-on-Bedrock both work (whichever the resolved model's
    ``chat_model_meta.provider`` says) via the same adapter-selection agents/
    headless already do — on-device (``local:*``) models are refused, same
    reasoning and message as run_headless_turn: cold-start
    (llama_server.ensure_serving) + capability probing is a separate piece
    of work this migration doesn't do.
  - The old per-tool 180-second watchdog is gone (chat/agents never had one
    either — the shared tool-execution path has no per-call deadline). No
    whole-run wall-clock deadline exists today to wire into
    TurnPolicy.deadline_seconds, so it stays None, matching CHAT_POLICY.
  - Cost recording per round and the near-cap wind-down reminder are now
    handled generically by run_turn itself; the old manual duplicates of
    both are gone.

What did NOT change: ``_assemble_cron_tools`` (still drops
``ask_user_question`` — an unattended run can never answer it),
``_merge_notifications``, the per-job model override rule (still
Anthropic-only; broadening that is a separate decision, not part of this
migration), ``cron_max_rounds`` config/default, the WS-E verify-and-continue
call shape (server.goals.cron_verify.verify, same MAX_CONTINUATIONS=2
semantics, judged on every natural finish including the last allowed round),
and how results land in cron_runs / the cron-inbox session / the
cron_progress SSE frames.
"""

import asyncio
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace as _dc_replace
from datetime import datetime, timezone

log = logging.getLogger("whisper-studio")

# Default tool-round cap for an unattended run; overridable via the
# `cron_max_rounds` config key (was a bare magic 15 before).
CRON_MAX_ROUNDS_DEFAULT = 30

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
    through unchanged — aws_boto3 is a native tool now, always present in the
    assembled pool with its canonical schema, so the old always-inject shadow
    definition (from when it was a disableable skill) is gone.
    """
    return [t for t in pool if t["name"] != "ask_user_question"]


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
    now works, replacing the old thread's 1-second poll.
    """
    # cron_scheduler owns load_cron_jobs, _push_result, the in-progress state,
    # and _server_loop. Import it lazily (a module-top import would be a cycle:
    # cron_scheduler re-exports this module's functions).
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
    started = datetime.now(timezone.utc)
    session_id = job.get("session_id", "")

    try:
        from server import cron_history

        cron_history.start_run(run_id, job_id, job.get("name", ""), session_id)
    except Exception as exc:
        log.warning("cron: failed to open run lease: %s", exc)

    def _elapsed_ms() -> int:
        return int((datetime.now(timezone.utc) - started).total_seconds() * 1000)

    # Captured notify_user messages, folded into whichever exit path fires.
    # Bound before the try so the exception handler can always read it, even
    # on an early failure.
    notifications: list[str] = []
    try:
        from server.chat.tool_pool import assemble_tool_pool
        from server.infrastructure.config import load_config
        from server.local.runtime import is_local_model_id
        from server.workspace import get_workspace_path

        config = load_config()
        chat_models = config.get("chat_models", {})
        # Per-job model override: an explicit `model` key on the job wins when
        # it resolves to an Anthropic Bedrock id; otherwise the haiku-first
        # fallback. Unchanged from before this migration — broadening this to
        # OpenAI-on-Bedrock is a separate decision, not part of it (see the
        # module docstring and the migration report).
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
            for candidate in ("haiku", "sonnet"):
                if chat_models.get(candidate):
                    model_key = candidate
                    model_id = chat_models[candidate]
                    break
            else:
                if not chat_models:
                    raise RuntimeError(
                        "no chat models configured: chat_models is empty "
                        "(check config.json / config.example.json)"
                    )
                model_key, model_id = next(iter(chat_models.items()))

        if is_local_model_id(model_id):
            # On-device models need their own cold-start (llama_server.
            # ensure_serving) and capability probing (supports_tools/
            # supports_thinking) — a materially separate piece of work this
            # migration doesn't do. Matches run_headless_turn's identical
            # refusal (server/exec/headless.py) for the identical reason.
            # _validate_job_model already keeps an explicit override from
            # reaching here (a local id never contains "anthropic"); this is
            # the defensive backstop for the haiku/sonnet/any-configured
            # fallback chain, which doesn't otherwise check.
            raise RuntimeError(
                f"model {model_id!r} is on-device (local:*), which scheduled "
                "runs cannot use yet — it needs its own cold-start/capability "
                "wiring (see run_headless_turn's identical restriction)"
            )

        max_rounds = CRON_MAX_ROUNDS_DEFAULT
        try:
            max_rounds = max(1, min(int(config.get("cron_max_rounds", max_rounds)), 200))
        except (TypeError, ValueError):
            pass

        # Give the scheduled run the same tool pool an interactive chat turn
        # gets, built ONCE for the whole run (mirrors agents/runtime.py).
        pool = assemble_tool_pool(
            plan_mode=False,
            ws_connected=bool(get_workspace_path()),
            suppress_workspace_search=False,
        )
        cron_tools = _assemble_cron_tools(pool)

        def _tool_catalog() -> tuple[list[dict], int | None]:
            return cron_tools, None

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
        from server.chat.infra import _get_chat_model_meta

        _provider = _get_chat_model_meta().get(model_key, {}).get("provider", "anthropic")
        if _provider == "openai_bedrock":
            from server.chat.engine.openai import OpenAIResponsesAdapter

            adapter = OpenAIResponsesAdapter(
                model_key=model_key,
                model_id=model_id,
                system_prompt=system,
                effort_label=None,
                session_id=session_id,
            )
        else:
            from server.chat.caching import cache_ttl_for
            from server.chat.engine.anthropic import AnthropicAdapter

            adapter = AnthropicAdapter(
                model_key=model_key,
                model_id=model_id,
                system_prompt=system,
                # The whole system prompt is the "static" cacheable block —
                # cron has no per-turn dynamic RAG portion the way chat does.
                # Matches the old loop's own cached_tools_and_system(...,
                # system, "", ttl) split exactly.
                system_static=system,
                system_dynamic="",
                caching_on=_caching_on,
                cache_ttl=cache_ttl_for(model_id),
                effort_label=None,
                force_skill=None,
                loop=loop,
                executor=_CRON_EXECUTOR,
            )

        from server.chat.engine.policy import TurnPolicy
        from server.chat.engine.runner import TurnContext, run_turn
        from server.goals.cron_verify import MAX_CONTINUATIONS
        from server.goals.cron_verify import verify as _cron_verify

        messages = [{"role": "user", "content": job["prompt"]}]

        ctx = TurnContext(
            session_id=session_id,
            model_key=model_key,
            model_id=model_id,
            messages=messages,
            adapter=adapter,
            policy=TurnPolicy(
                max_rounds=max_rounds,
                # No whole-run wall-clock deadline concept exists today (only
                # the round cap did) — leave None, matching CHAT_POLICY.
                deadline_seconds=None,
                # Cron keeps its OWN verify-and-continue below, called as an
                # explicit step after each run_turn call — never the engine's
                # automatic completion_gate (Stop hooks + goal evaluator).
                # Exactly like agents: an unattended run must never gain NEW
                # Stop-hook exposure as a side effect of this migration.
                completion_gate=False,
                salvage_round=True,
            ),
            loop=loop,
            executor=_CRON_EXECUTOR,
            tool_exec_model_id=model_id,
            # Cron never ran interactive chat's post-turn memory hooks (auto
            # memory / session memory / dream consolidation) before either.
            memory_hooks=lambda msgs: None,
            unattended=True,
            turn_scope_id=f"cron:{job_id}",
            tool_catalog=_tool_catalog,
        )

        # ── Drain run_turn, possibly more than once (WS-E verify-and-continue
        # calls it again over the accumulated messages when the prompt isn't
        # satisfied yet) ── THIS PARSING IS COUPLED TO runner.py's PINNED SSE
        # FRAME VOCABULARY, exactly like _run_agent_loop's/run_headless_turn's
        # own drains — see runner.py's module docstring before touching this.
        turn_no = 0  # rounds consumed so far, across every run_turn() call
        final_text_parts: list[str] = []
        round_text_parts: list[str] = []
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
            if remaining <= 0:
                break
            if ctx.policy.max_rounds != remaining:
                # Each continuation gets what's LEFT of the original budget,
                # not a fresh one — the total round budget across every
                # verify-driven continuation stays cron_max_rounds, exactly
                # like the old single hand-rolled loop.
                ctx.policy = _dc_replace(ctx.policy, max_rounds=remaining)

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
                        turn_no += 1
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
                # A terminal round-level error — skip verify entirely (mirrors
                # the old loop's behavior: an exception went straight to
                # failure, never through the verifier) and fall through to
                # the failure exit below.
                break

            if not _ff_enabled("cron_verify"):
                break

            # WS-E: verify the run actually satisfied its prompt before
            # reporting success — on EVERY natural finish (a run finishing on
            # its last allowed round is verified too; only the continuation
            # is round-gated). Synchronous and safe to block a worker thread
            # on (never the event loop) — same reasoning as
            # server.goals.gate.run_completion_gate's own evaluator call.
            verdict = await loop.run_in_executor(
                _CRON_EXECUTOR, _cron_verify, job["prompt"], ctx.messages, notifications
            )
            if (
                verdict.is_achieved
                or verify_continuations >= MAX_CONTINUATIONS
                or turn_no >= max_rounds
            ):
                break
            verify_continuations += 1
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
            result_text = f"[UNVERIFIED] {verdict.feedback}\n\n{result_text}"
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
        # task). Report cleanly, same shape as the old thread's cooperative
        # exit, then re-raise so the task ends genuinely cancelled (mirrors
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
        try:
            cron_events.emit_progress(
                session_id,
                run_id=run_id,
                job_name=job.get("name", ""),
                phase="failed",
            )
        except Exception:
            pass
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
