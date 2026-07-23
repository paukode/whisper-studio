"""
Bedrock run loop for scheduled cron jobs.

Extracted from ``cron_scheduler.py`` to keep that runtime file under the size
budget. This module owns the unattended execution path: assembling the tool
list, running the Bedrock tool-use loop, and folding captured notify_user
messages into the pushed result.

Names owned by ``cron_scheduler`` (``load_cron_jobs``, ``_push_result``,
``_server_loop``, the in-progress concurrency state) are reached LAZILY via
``from server import cron_scheduler as _cs`` inside the function body, so
importing this module never triggers a circular import at load time. In
particular ``_cs._server_loop`` is read fresh on every call because
``init_scheduler`` reassigns it.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

log = logging.getLogger("whisper-studio")

# Default tool-round cap for an unattended run; overridable via the
# `cron_max_rounds` config key (was a bare magic 15 before).
CRON_MAX_ROUNDS_DEFAULT = 30


def _cron_pre_hook(tool_name: str, tool_input: dict, session_id: str, model_id: str):
    """Run the PreToolUse hook chain from this worker thread by bridging onto
    the server event loop (same pattern as the route_tool call). Returns the
    HookOutcome, or None if hooks can't run (never blocks the tool on infra
    failure)."""
    from server import cron_scheduler as _cs
    from server.hooks import run_hooks

    if _cs._server_loop is None:
        return None
    fut = None
    try:
        fut = asyncio.run_coroutine_threadsafe(
            run_hooks(
                "PreToolUse",
                {
                    "event": "PreToolUse",
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "session_id": session_id,
                    "model_id": model_id,
                },
                tool_name=tool_name,
            ),
            _cs._server_loop,
        )
        # Comfortably exceeds the max single-hook timeout (60s) so a slow but
        # legitimate deny isn't spuriously bypassed.
        return fut.result(timeout=90)
    except Exception as e:
        # Cancel so the bridged coroutine tears down instead of running
        # detached (it would otherwise leak on a timeout).
        if fut is not None:
            fut.cancel()
        log.warning("Cron PreToolUse hook failed for %s: %s", tool_name, e)
        return None


def _finish_stopped(
    job: dict, notifications: list[str], run_id: str, elapsed_ms: int, session_id: str
) -> None:
    """Shared exit for a user-requested stop: progress frame + result push."""
    from server import cron_events
    from server import cron_scheduler as _cs

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
        duration_ms=elapsed_ms,
    )


# ── Bedrock execution ────────────────────────────────────────────────────────


def _assemble_cron_tools(pool: list[dict]) -> list[dict]:
    """Tool list for an unattended cron InvokeModel call.

    aws_boto3 is always made available to cron runs (even if its skill is
    disabled in the catalog) by prepending our own definition. The aws_boto3
    skill is ALSO part of the assembled pool, so it is filtered out before
    prepending — otherwise the tool is advertised twice and Bedrock rejects the
    request with "Tool names must be unique". ask_user_question is dropped too:
    it pauses for a human reply that never comes in an unattended run and would
    hang the job forever.
    """
    aws_boto3_tool = {
        "name": "aws_boto3",
        "description": "Execute a read-only AWS boto3 API call.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "method": {"type": "string"},
                "params": {"type": "object", "default": {}},
                "region": {"type": "string", "default": "us-east-1"},
            },
            "required": ["service", "method"],
        },
    }
    excluded = {"aws_boto3", "ask_user_question"}
    return [aws_boto3_tool] + [t for t in pool if t["name"] not in excluded]


def _merge_notifications(notifications: list[str], tail: str) -> str:
    """Fold captured notify_user messages into a pushed result.

    All four exit paths (success + the three failures) route their text through
    here so a report delivered via notify_user (a side-effect, not tool output)
    is never dropped. Delivered blocks come first, then ``tail`` — the model's
    final text on success, or the status marker on failure. Empties and exact
    dupes are dropped (a model often both notifies AND echoes the same report).
    """
    blocks: list[str] = []
    seen: set[str] = set()
    for part in [*notifications, tail]:
        part = (part or "").strip()
        if part and part not in seen:
            seen.add(part)
            blocks.append(part)
    return "\n\n".join(blocks) if blocks else "(no output)"


def _execute_cron_prompt(job_id: str):
    """Execute a cron job's prompt via Bedrock with a tool-use loop.
    Re-reads the job from disk to get the latest session_id/prompt."""
    # cron_scheduler owns load_cron_jobs, _push_result, the in-progress state,
    # and _server_loop. Import it lazily (a module-top import would be a cycle:
    # cron_scheduler re-exports this module's functions). _server_loop is read
    # fresh via _cs each call because init_scheduler reassigns it.
    from server import cron_scheduler as _cs

    jobs = _cs.load_cron_jobs()
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if not job:
        log.error("Cron execute: job %s not found", job_id)
        return

    # Concurrency guard: never run the same job twice at once (covers the
    # run-now vs scheduled-fire race; scheduled overlap is also blocked by
    # APScheduler max_instances=1).
    from server import cron_events

    with _cs._IN_PROGRESS_LOCK:
        if job_id in _cs._in_progress:
            log.info("Cron '%s' already running; skipping duplicate", job.get("name"))
            return
        _cs._in_progress.add(job_id)
        # Register the stop flag at the same instant the job is marked
        # in-progress: a stop request in the setup window (config/tool-pool
        # assembly) must not be silently lost.
        cron_events.open_run(job_id)

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

    # Captured notify_user messages, folded into whichever exit path fires (all
    # four route through _merge_notifications). Bound before the try so the
    # exception handler can always read it, even on an early failure.
    notifications: list[str] = []
    pool_executor = None
    try:
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        import boto3
        from botocore.config import Config as BotoConfig

        # Lazy imports: server.chat.tool_pool imports CRON_TOOLS from
        # cron_scheduler, so importing the pool/router at module top would be a
        # circular import. Import inside the function instead.
        from server.chat.tool_pool import assemble_tool_pool
        from server.infrastructure.config import load_config
        from server.tool_router import SIDE_EFFECT_PAUSE, route_tool
        from server.workspace import get_workspace_path

        config = load_config()
        region = config.get("bedrock_region", "us-east-1")
        chat_models = config.get("chat_models", {})
        # Per-job model override: an explicit `model` key on the job wins when
        # it resolves to an Anthropic Bedrock id (this loop only speaks the
        # Anthropic InvokeModel API); otherwise the haiku-first fallback.
        model_key = ""
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
        max_rounds = CRON_MAX_ROUNDS_DEFAULT
        try:
            max_rounds = max(1, min(int(config.get("cron_max_rounds", max_rounds)), 200))
        except (TypeError, ValueError):
            pass

        client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=BotoConfig(read_timeout=120, connect_timeout=10, retries={"max_attempts": 2}),
        )

        # Give the scheduled run the same tool pool an interactive chat turn
        # gets: MCP servers honoured by their persisted `enabled` flag (so the
        # AgentCore browser is available when enabled), web search, tasks,
        # agents, skills, and — only when a workspace is open — workspace/git
        # tools. This mirrors chat's own `ws_connected=bool(ws_path)` rule.
        pool = assemble_tool_pool(
            plan_mode=False,
            ws_connected=bool(get_workspace_path()),
            mcp_enabled_names=None,
            suppress_workspace_search=False,
        )
        cron_tools = _assemble_cron_tools(pool)
        pool_executor = ThreadPoolExecutor(max_workers=4)

        from server.prompts.rules import append_rules

        messages = [{"role": "user", "content": job["prompt"]}]
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

        _verify_conts = 0  # WS-E: how many times the verify pass extended the run
        for round_num in range(max_rounds):
            if cron_events.stop_requested(job_id):
                _finish_stopped(job, notifications, run_id, _elapsed_ms(), session_id)
                return
            # Wind-down so the model consolidates instead of hitting the cap.
            if max_rounds - round_num == 3:
                from server.chat.loop_hints import inject_reminder

                inject_reminder(
                    messages,
                    "<system-reminder>Only 3 tool rounds remain for this "
                    "scheduled run. Consolidate and produce the final report "
                    "now; do not open new lines of work.</system-reminder>",
                )
            cron_events.emit_progress(
                session_id,
                run_id=run_id,
                job_name=job.get("name", ""),
                phase="turn_start",
                turn=round_num + 1,
                max_turns=max_rounds,
            )
            body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 16384,
                "system": system,
                "messages": messages,
                "tools": cron_tools,
            }
            if _caching_on and cron_tools and system:
                from server.chat.caching import (
                    annotate_messages_cache,
                    cache_ttl_for,
                    cached_tools_and_system,
                )

                _ttl = cache_ttl_for(model_id)
                body["tools"], body["system"] = cached_tools_and_system(
                    cron_tools, system, "", _ttl
                )
                body["messages"] = annotate_messages_cache(messages, _ttl)
            response = client.invoke_model(
                modelId=model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )
            result = json.loads(response["body"].read())
            stop_reason = result.get("stop_reason", "end_turn")
            content = result.get("content", [])
            messages.append({"role": "assistant", "content": content})
            # Cron rounds were invisible in /api/costs; record them like chat
            # rounds so cache telemetry and budgets see scheduled work too.
            try:
                from server.costs.tracker import estimate_cost as _est
                from server.costs.tracker import record_turn as _rec

                usage = result.get("usage", {}) or {}
                _in = usage.get("input_tokens", 0)
                _out = usage.get("output_tokens", 0)
                _cr = usage.get("cache_read_input_tokens", 0)
                _cw = usage.get("cache_creation_input_tokens", 0)
                _rec(
                    session_id=session_id,
                    turn_number=round_num,
                    model=model_key,
                    input_tokens=_in,
                    output_tokens=_out,
                    cost_usd=_est(model_key, _in, _out, _cr, _cw),
                    cache_read_tokens=_cr,
                    cache_creation_tokens=_cw,
                )
            except Exception as exc:
                log.debug("cron: cost record failed: %s", exc)

            if stop_reason != "tool_use":
                text_parts = [b["text"] for b in content if b.get("type") == "text"]
                final_text = "\n".join(text_parts).strip()

                # WS-E: verify the run actually satisfied its prompt before
                # reporting success — on EVERY exit round (a run finishing on its
                # last allowed round is verified too; only the continuation is
                # round-gated). notify_user bodies are passed to the verifier —
                # they are the deliverable channel. An unmet verdict extends the
                # run (budget + rounds permitting) or pushes status=failed with
                # an [UNVERIFIED] prefix (report content preserved).
                _verdict = None
                if _ff_enabled("cron_verify"):
                    from server.goals.cron_verify import MAX_CONTINUATIONS
                    from server.goals.cron_verify import verify as _cron_verify

                    _verdict = _cron_verify(job["prompt"], messages, notifications)
                    if (
                        not _verdict.is_achieved
                        and _verify_conts < MAX_CONTINUATIONS
                        and round_num < max_rounds - 1
                    ):
                        _verify_conts += 1
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"[verify] {_verdict.feedback} The task is not yet "
                                    "complete. Keep working until it is genuinely done, "
                                    "then output the finished result."
                                ),
                            }
                        )
                        continue

                result_text = _merge_notifications(notifications, final_text)
                status = "ok"
                if _verdict is not None and not _verdict.is_achieved:
                    result_text = f"[UNVERIFIED] {_verdict.feedback}\n\n{result_text}"
                    status = "failed"
                cron_events.emit_progress(
                    session_id,
                    run_id=run_id,
                    job_name=job.get("name", ""),
                    phase="completed",
                    turn=round_num + 1,
                )
                _cs._push_result(
                    job, result_text, status=status, run_id=run_id, duration_ms=_elapsed_ms()
                )
                log.info(
                    "Cron '%s' result pushed (session=%s, status=%s)",
                    job.get("name"),
                    session_id,
                    status,
                )
                return

            tool_results = []
            paused = False
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                if cron_events.stop_requested(job_id):
                    _finish_stopped(job, notifications, run_id, _elapsed_ms(), session_id)
                    return
                cron_events.emit_progress(
                    session_id,
                    run_id=run_id,
                    job_name=job.get("name", ""),
                    phase="tool_call",
                    turn=round_num + 1,
                    tool_name=block.get("name", ""),
                    tool_input_preview=str(block.get("input", {}))[:200],
                )
                side_effects: list = []
                if _cs._server_loop is None:
                    output = (
                        "Error: server event loop unavailable; cannot run tools in this cron run."
                    )
                else:
                    # route_tool is async and offloads to `executor`; run it on
                    # the server loop from this worker thread and block for the
                    # result, the same bridge _push_result uses.
                    #
                    # Inject the run's session id so session-scoped executors act
                    # on the owning session, not the empty one. Copy the input
                    # (like the agent runtime) so the internal __session_id__ key
                    # never leaks into block["input"] — the dict replayed to the
                    # model on every later round.
                    call_input = dict(block["input"])
                    call_input["__session_id__"] = session_id
                    # PreToolUse gate — scheduled runs honor blocking hooks too.
                    _pre = _cron_pre_hook(block["name"], dict(block["input"]), session_id, model_id)
                    if _pre is not None and _pre.blocked:
                        output, side_effects = f"[Hook denied] {_pre.reason}", []
                    else:
                        if _pre is not None and _pre.updated_input is not None:
                            call_input = dict(_pre.updated_input)
                            call_input["__session_id__"] = session_id
                        try:
                            fut = asyncio.run_coroutine_threadsafe(
                                route_tool(
                                    block["name"],
                                    call_input,
                                    loop=_cs._server_loop,
                                    executor=pool_executor,
                                    transcript="",
                                    attachments=None,
                                    session_id=session_id,
                                    model_id=model_id,
                                    tool_use_id=block["id"],
                                    origin="cron",
                                ),
                                _cs._server_loop,
                            )
                            # 1s-slice poll instead of one 180s block, so a stop
                            # request abandons an in-flight tool within ~1s.
                            output = side_effects = None
                            for _tick in range(180):
                                try:
                                    output, side_effects = fut.result(timeout=1)
                                    break
                                except FuturesTimeoutError:
                                    if cron_events.stop_requested(job_id):
                                        fut.cancel()
                                        _finish_stopped(
                                            job, notifications, run_id, _elapsed_ms(), session_id
                                        )
                                        return
                            if output is None and side_effects is None:
                                # The tool didn't finish inside the budget. Cancel
                                # the future so the coroutine tears down instead of
                                # running detached, then hand the model an error.
                                fut.cancel()
                                output, side_effects = (
                                    "Error: tool call timed out after 180s and was cancelled",
                                    [],
                                )
                        except Exception as e:
                            output, side_effects = f"Error: {e}", []
                # A tool that needs interactive approval returns a raw
                # [WS_APPROVAL]<json> sentinel. A scheduled run has no human to
                # approve it, so treat it exactly like a pause: stop cleanly via
                # the existing paused/_push_result path, and never hand the raw
                # magic string to the model (or any log) as the tool's output.
                if isinstance(output, str) and output.startswith("[WS_APPROVAL]"):
                    paused = True
                    result_content = (
                        "[not run] this tool requires interactive approval, "
                        "which a scheduled run cannot provide."
                    )
                else:
                    result_content = str(output)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": result_content,
                    }
                )
                cron_events.emit_progress(
                    session_id,
                    run_id=run_id,
                    job_name=job.get("name", ""),
                    phase="tool_result",
                    turn=round_num + 1,
                    tool_name=block.get("name", ""),
                    output_preview=result_content[:200],
                )
                for se in side_effects:
                    if "notify_user" in se:
                        msg = (se["notify_user"] or {}).get("message", "")
                        if msg:
                            notifications.append(msg)
                # Defensive: ask_user_question is already stripped from the
                # pool, but if any handler ever signals a pause, stop cleanly
                # instead of looping until the round cap.
                if any(SIDE_EFFECT_PAUSE in se for se in side_effects):
                    paused = True
            messages.append({"role": "user", "content": tool_results})
            if paused:
                _cs._push_result(
                    job,
                    _merge_notifications(
                        notifications,
                        "[stopped] a tool requested user input, "
                        "which isn't available in a scheduled run.",
                    ),
                    status="failed",
                    run_id=run_id,
                    duration_ms=_elapsed_ms(),
                )
                return

        cron_events.emit_progress(
            session_id,
            run_id=run_id,
            job_name=job.get("name", ""),
            phase="failed",
            turn=max_rounds,
        )
        _cs._push_result(
            job,
            _merge_notifications(notifications, f"[FAILED] max tool rounds reached ({max_rounds})"),
            status="failed",
            run_id=run_id,
            duration_ms=_elapsed_ms(),
        )

    except Exception as e:
        log.error("Cron execution failed for '%s': %s", job.get("name"), e)
        try:
            from server import cron_events as _ce

            _ce.emit_progress(
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
        try:
            from server import cron_events as _ce

            _ce.close_run(job_id)
        except Exception:
            pass
        if pool_executor is not None:
            # cancel_futures drops any tool call still queued/in-flight (e.g. a
            # timed-out one) instead of leaving worker threads running past the
            # run's lifetime.
            pool_executor.shutdown(wait=False, cancel_futures=True)
        with _cs._IN_PROGRESS_LOCK:
            _cs._in_progress.discard(job_id)
