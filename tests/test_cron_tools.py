"""Tests for cron InvokeModel tool assembly (`_assemble_cron_tools`).

Regression cover for the "Tool names must be unique" Bedrock error: the
aws_boto3 skill is part of the assembled chat tool pool, and cron also prepends
its own aws_boto3 definition, so a naive concatenation advertised the tool
twice and failed every scheduled run.
"""

import asyncio
import json
import threading

import server.cron_scheduler as C


def _tool(name):
    return {"name": name, "description": name, "input_schema": {"type": "object"}}


def test_aws_boto3_not_duplicated_when_pool_has_the_skill():
    # The pool already contains aws_boto3 (from the skill catalog). cron must
    # advertise it exactly once, or Bedrock rejects InvokeModel.
    pool = [_tool("aws_boto3"), _tool("web_search"), _tool("ask_user_question")]
    names = [t["name"] for t in C._assemble_cron_tools(pool)]
    assert names.count("aws_boto3") == 1
    assert len(names) == len(set(names))


def test_ask_user_question_is_dropped():
    # Unattended runs can never answer it; leaving it in would hang the job.
    pool = [_tool("web_search"), _tool("ask_user_question")]
    names = [t["name"] for t in C._assemble_cron_tools(pool)]
    assert "ask_user_question" not in names
    assert "web_search" in names


def test_aws_boto3_present_even_when_skill_disabled():
    # If the aws_boto3 skill is disabled, the pool won't contain it, but cron
    # must still expose aws_boto3 with a valid schema.
    pool = [_tool("web_search")]
    tools = C._assemble_cron_tools(pool)
    aws = [t for t in tools if t["name"] == "aws_boto3"]
    assert len(aws) == 1
    assert aws[0]["input_schema"]["required"] == ["service", "method"]


# ── Approval sentinel in an unattended run ────────────────────────────────────


class _FakeBody:
    """Mimics the botocore StreamingBody returned in response['body']."""

    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw


class _FakeBedrock:
    """Returns one tool_use round, then end_turn. Records every request body so
    the test can assert the raw [WS_APPROVAL] sentinel is never sent to the
    model."""

    def __init__(self):
        self.bodies: list[str] = []

    def invoke_model(self, **kwargs):
        self.bodies.append(kwargs.get("body", ""))
        if len(self.bodies) == 1:
            payload = {
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "terminal_run",
                        "input": {"command": "echo hi"},
                    }
                ],
            }
        else:
            payload = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}
        return {"body": _FakeBody(payload)}


def test_approval_sentinel_stops_run_and_is_not_sent_to_model(monkeypatch):
    """A tool that requires interactive approval returns a raw [WS_APPROVAL]
    sentinel. A scheduled run has no human to approve it, so the run must stop
    cleanly with a failed "[stopped]" result and must never hand the raw
    sentinel string back to the model as tool output.
    """
    import boto3

    import server.chat.tool_pool as TP
    import server.cron_history as H
    import server.infrastructure.config as CFG
    import server.prompts.rules as R
    import server.tool_router as TR
    import server.workspace as WS

    job = {
        "id": "job-approval",
        "name": "approval-job",
        "prompt": "do the thing",
        "session_id": "sess-1",
        "schedule": {"type": "interval", "seconds": 1800},
        "enabled": True,
    }

    fake_client = _FakeBedrock()

    async def fake_route_tool(name, tool_input, **kwargs):
        # This is exactly what an approval-gated executor returns.
        return ('[WS_APPROVAL]{"tool": "terminal_run"}', [])

    monkeypatch.setattr(C, "load_cron_jobs", lambda: [job])
    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake_client)
    monkeypatch.setattr(TP, "assemble_tool_pool", lambda **k: [])
    monkeypatch.setattr(WS, "get_workspace_path", lambda: "")
    monkeypatch.setattr(R, "append_rules", lambda s: s)
    monkeypatch.setattr(H, "start_run", lambda *a, **k: None)
    monkeypatch.setattr(TR, "route_tool", fake_route_tool)
    monkeypatch.setattr(
        CFG,
        "load_config",
        lambda: {
            "bedrock_region": "us-east-1",
            "chat_models": {"haiku": "fake-model-id"},
            "cron_max_runs_per_job": 200,
            "cron_misfire_grace_sec": 3600,
            # Loop-mechanics test; keep the completion verifier out of it
            # (see the flag in _run_cron_job below).
            "feature_flags": {"cron_verify": False},
        },
    )

    recorded: dict = {}

    def fake_push(job, text, status="ok", *, run_id, duration_ms=None):
        recorded["text"] = text
        recorded["status"] = status

    monkeypatch.setattr(C, "_push_result", fake_push)

    # route_tool is dispatched onto _server_loop via run_coroutine_threadsafe,
    # so it must be a live, running loop on another thread.
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    monkeypatch.setattr(C, "_server_loop", loop)
    try:
        C._execute_cron_prompt("job-approval")
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2.0)
        loop.close()

    # The run stopped cleanly with a failed "[stopped]" result.
    assert recorded.get("status") == "failed"
    assert recorded.get("text", "").startswith("[stopped]")

    # The loop stopped immediately; it never fed the sentinel back for another
    # round (a second invoke would carry the tool_result content).
    assert len(fake_client.bodies) == 1

    # And the raw magic string was never sent to the model in any request.
    assert all("[WS_APPROVAL]" not in body for body in fake_client.bodies)


# ── Shared harness for driving a full _execute_cron_prompt run ────────────────


def _run_cron_job(monkeypatch, job, fake_client, fake_route_tool, recorded, *, cron_verify=False):
    """Wire the lazy imports _execute_cron_prompt reaches for, run the job on a
    live background loop (route_tool is dispatched there), and capture the
    pushed result into ``recorded``.

    ``cron_verify`` defaults OFF: these loop-mechanics tests don't stub the
    completion verifier, whose evaluator would make a real one-shot Bedrock
    call. test_goal_cron_verify.py opts back in after stubbing verify()."""
    import boto3

    import server.chat.tool_pool as TP
    import server.cron_history as H
    import server.infrastructure.config as CFG
    import server.prompts.rules as R
    import server.tool_router as TR
    import server.workspace as WS

    monkeypatch.setattr(C, "load_cron_jobs", lambda: [job])
    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake_client)
    monkeypatch.setattr(TP, "assemble_tool_pool", lambda **k: [])
    monkeypatch.setattr(WS, "get_workspace_path", lambda: "")
    monkeypatch.setattr(R, "append_rules", lambda s: s)
    monkeypatch.setattr(H, "start_run", lambda *a, **k: None)
    monkeypatch.setattr(TR, "route_tool", fake_route_tool)
    monkeypatch.setattr(
        CFG,
        "load_config",
        lambda: {
            "bedrock_region": "us-east-1",
            "chat_models": {"haiku": "fake-model-id"},
            "cron_max_runs_per_job": 200,
            "cron_misfire_grace_sec": 3600,
            # The verifier's evaluator does a real one-shot Bedrock call
            # through the process-wide client cache in server.chat.infra,
            # sidestepping this harness's boto3 patch whenever an earlier test
            # already cached a client (a live Haiku correctly judges these
            # synthetic transcripts unfinished and flips the status to failed).
            # So the flag stays off unless the caller stubbed verify().
            "feature_flags": {"cron_verify": cron_verify},
        },
    )

    def fake_push(job, text, status="ok", *, run_id, duration_ms=None):
        recorded["text"] = text
        recorded["status"] = status

    monkeypatch.setattr(C, "_push_result", fake_push)

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    monkeypatch.setattr(C, "_server_loop", loop)
    try:
        C._execute_cron_prompt(job["id"])
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2.0)
        loop.close()


def test_failure_exit_includes_captured_notify_user(monkeypatch):
    """A notify_user message captured during a run that then takes a FAILURE
    exit must be folded into the pushed result, not dropped. (fix 1)"""
    from server.tool_router import SIDE_EFFECT_PAUSE

    notif = "REPORT: all endpoints healthy — https://example.com/status"

    job = {
        "id": "job-notify-fail",
        "name": "notify-fail",
        "prompt": "do the thing",
        "session_id": "sess-9",
        "schedule": {"type": "interval", "seconds": 1800},
        "enabled": True,
    }

    async def fake_route_tool(name, tool_input, **kwargs):
        # Deliver a report via notify_user (a side-effect, not tool output),
        # then signal a pause so the run takes a failure exit.
        return ("ok", [{"notify_user": {"message": notif}}, {SIDE_EFFECT_PAUSE: True}])

    recorded: dict = {}
    _run_cron_job(monkeypatch, job, _FakeBedrock(), fake_route_tool, recorded)

    assert recorded.get("status") == "failed"
    # The notify_user content survives...
    assert notif in recorded.get("text", "")
    # ...alongside the status marker for the failure exit.
    assert "[stopped]" in recorded.get("text", "")


def test_timed_out_tool_call_is_cancelled(monkeypatch):
    """A tool call that exceeds the 180s budget must have its future cancelled
    (so run_coroutine_threadsafe tears the coroutine down rather than leaving it
    running detached) and an error substituted for the model. (fix 1)

    The 180s timeout is hardcoded, so instead of waiting we fake
    ``asyncio.run_coroutine_threadsafe`` to hand back a future whose ``result``
    raises TimeoutError and that records ``cancel()`` calls.
    """
    import asyncio as _asyncio
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    job = {
        "id": "job-timeout",
        "name": "timeout-job",
        "prompt": "do the thing",
        "session_id": "sess-t",
        "schedule": {"type": "interval", "seconds": 1800},
        "enabled": True,
    }

    cancelled = {"count": 0}

    from server.hooks.schema import HookOutcome

    class _FakeFuture:
        def result(self, timeout=None):
            raise FuturesTimeoutError()

        def cancel(self):
            cancelled["count"] += 1
            return True

    class _AllowFuture:
        """The PreToolUse pre-hook bridge resolves quickly with an allow."""

        def result(self, timeout=None):
            return HookOutcome()

        def cancel(self):
            return False

    def fake_rcts(coro, loop):
        # The cron loop bridges two coroutines onto the server loop: the
        # PreToolUse hook chain (which normally resolves fast) and the tool
        # call itself. Only the tool call is being timed out here.
        name = getattr(getattr(coro, "cr_code", None), "co_name", "")
        # Close the real coroutine so Python doesn't warn "never awaited".
        try:
            coro.close()
        except Exception:
            pass
        return _AllowFuture() if name == "run_hooks" else _FakeFuture()

    async def fake_route_tool(name, tool_input, **kwargs):
        return ("unused", [])

    monkeypatch.setattr(_asyncio, "run_coroutine_threadsafe", fake_rcts)

    fake_client = _FakeBedrock()  # one tool_use round, then end_turn
    recorded: dict = {}
    _run_cron_job(monkeypatch, job, fake_client, fake_route_tool, recorded)

    # The timed-out call's future was cancelled exactly once.
    assert cancelled["count"] == 1
    # The run kept going (the model got an error tool_result) and finished.
    assert recorded.get("status") == "ok"
    # The timeout error was fed back as the tool_result on round 2.
    assert "timed out" in fake_client.bodies[1]


def test_route_tool_receives_session_id_without_leaking(monkeypatch):
    """Cron runs inject the owning session id into every tool input so
    session-scoped executors work, but via a COPY so the internal
    __session_id__ key never leaks into the replayed transcript. (fix 5)"""
    job = {
        "id": "job-session",
        "name": "session-job",
        "prompt": "do the thing",
        "session_id": "sess-5",
        "schedule": {"type": "interval", "seconds": 1800},
        "enabled": True,
    }

    captured: dict = {}

    async def fake_route_tool(name, tool_input, **kwargs):
        captured["input"] = dict(tool_input)
        return ("done", [])

    fake_client = _FakeBedrock()  # one tool_use round, then end_turn
    recorded: dict = {}
    _run_cron_job(monkeypatch, job, fake_client, fake_route_tool, recorded)

    # The executor saw the run's session id.
    assert captured.get("input", {}).get("__session_id__") == "sess-5"
    # The run completed normally (end_turn on the second round).
    assert recorded.get("status") == "ok"
    # The sentinel never leaked into the assistant message replayed on round 2
    # (bodies[1] carries the tool_use block's original input).
    assert len(fake_client.bodies) == 2
    assert "__session_id__" not in fake_client.bodies[1]
