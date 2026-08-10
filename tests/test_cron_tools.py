"""Tests for cron's run loop: tool assembly (`_assemble_cron_tools`) and the
turn-engine-backed execution path (`_execute_cron_prompt`).

The execution tests drive the REAL asyncio `_execute_cron_prompt` end to end
(no mocking of the function itself) against a scripted streaming Bedrock
client — the same golden_harness.FakeBedrockClient interactive chat's golden
fixtures and the agent-runtime migration tests use, since cron's model call
now goes through the exact same server/chat/engine/anthropic.AnthropicAdapter.
"""

import asyncio
import json

import server.cron_scheduler as C
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block


def _tool(name):
    return {"name": name, "description": name, "input_schema": {"type": "object"}}


def test_aws_boto3_not_duplicated():
    # The pool always contains the native aws_boto3. cron must advertise it
    # exactly once, or Bedrock rejects InvokeModel.
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


def test_aws_boto3_comes_from_the_real_pool_with_canonical_schema():
    # aws_boto3 is a native tool: the REAL assembled pool always carries it,
    # and cron assembly must pass the canonical schema through untouched
    # (no private shadow copy).
    from server.chat.tool_pool import assemble_full_catalog
    from server.executors.tools import AWS_BOTO3_TOOL

    pool = assemble_full_catalog()
    tools = C._assemble_cron_tools(pool)
    aws = [t for t in tools if t["name"] == "aws_boto3"]
    assert len(aws) == 1
    assert aws[0] == AWS_BOTO3_TOOL
    assert aws[0]["input_schema"]["required"] == ["service", "method"]


# ── Shared harness for driving a full _execute_cron_prompt run ────────────────


def _run_cron_job(
    monkeypatch, job, fake_stream, fake_route_tool, recorded, *, cron_verify=False, tools=None
):
    """Wire the lazy imports _execute_cron_prompt reaches for, run the job
    through the real asyncio loop with a scripted Bedrock stream, and capture
    the pushed result into ``recorded``.

    ``cron_verify`` defaults OFF: these loop-mechanics tests don't stub the
    completion verifier, whose evaluator would make a real one-shot Bedrock
    call. test_goal_cron_verify.py opts back in after stubbing verify()."""
    import server.chat.tool_pool as TP
    import server.cron_history as H
    import server.infrastructure.config as CFG
    import server.prompts.rules as R
    import server.tool_executor as TE
    import server.workspace as WS

    monkeypatch.setattr(C, "load_cron_jobs", lambda: [job])
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    _tools = tools or []
    monkeypatch.setattr(TP, "assemble_partitioned_pool", lambda **k: (_tools, [], len(_tools)))
    monkeypatch.setattr(WS, "get_workspace_path", lambda: "")
    monkeypatch.setattr(R, "append_rules", lambda s: s)
    monkeypatch.setattr(H, "start_run", lambda *a, **k: None)
    monkeypatch.setattr(TE, "route_tool", fake_route_tool)
    monkeypatch.setattr(
        CFG,
        "load_config",
        lambda: {
            "chat_models": {"haiku": "fake-model-id"},
            "cron_max_runs_per_job": 200,
            "cron_misfire_grace_sec": 3600,
            # Loop-mechanics test; keep the completion verifier out of it
            # (see the flag in _run_cron_job below) unless the caller opted in.
            "feature_flags": {"cron_verify": cron_verify},
        },
    )

    def fake_push(job, text, status="ok", *, run_id, duration_ms=None):
        recorded["text"] = text
        recorded["status"] = status

    monkeypatch.setattr(C, "_push_result", fake_push)

    asyncio.run(C._execute_cron_prompt(job["id"]))


def test_approval_gated_tool_auto_executes_unattended(monkeypatch):
    """A tool that requires interactive approval returns a raw [WS_APPROVAL]
    sentinel. Cron now runs with TurnContext.unattended=True — the SAME
    contract a subagent gets — so this executes inline automatically instead
    of stopping the run: this is a deliberate, migration-driven behavior
    change from the old hand-rolled loop, which always failed the run rather
    than ever auto-approving. See server/tool_executor.py's
    process_tool_results / _execute_ws_approval_inline docstrings for the
    shared unattended contract this now inherits.
    """
    from server.approval import registry as approval_registry
    from server.approval.spec import ApprovalOutcome, ApprovalSpec

    job = {
        "id": "job-approval",
        "name": "approval-job",
        "prompt": "do the thing",
        "session_id": "sess-1",
        "schedule": {"type": "interval", "seconds": 1800},
        "enabled": True,
    }

    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("tu_1", "terminal_run", {"command": "echo hi"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("done"), *msg_end(stop_reason="end_turn")],
        ]
    )

    executed: dict = {}

    async def _fake_executor(payload):
        executed["payload"] = payload
        return ApprovalOutcome(ok=True, output="ran it")

    # A throwaway ApprovalSpec for "terminal_run" so _execute_ws_approval_
    # inline has something to dispatch to, without depending on the real
    # terminal-command executor's own side effects.
    fake_spec = ApprovalSpec(
        category="command",
        preview="command",
        summary="run it",
        executor=_fake_executor,
        risk_hint="low",
    )
    monkeypatch.setattr(approval_registry, "get", lambda action: fake_spec)

    async def fake_route_tool(name, tool_input, **kwargs):
        return ('[WS_APPROVAL]{"action": "terminal_run", "command": "echo hi"}', [])

    recorded: dict = {}
    _run_cron_job(monkeypatch, job, fake_stream, fake_route_tool, recorded)

    # Auto-executed (no human involved) — the run completes normally.
    assert recorded.get("status") == "ok"
    # The approval executor actually ran, stamped as an agent/unattended call.
    assert executed["payload"].get("__agent__") is True
    # The executor's real result reached the model as the tool_result...
    assert "ran it" in json.dumps(fake_stream.requests[1])
    # The raw internal sentinel never reached the model.
    assert all("[WS_APPROVAL]" not in json.dumps(r) for r in fake_stream.requests)


def test_failure_exit_includes_captured_notify_user(monkeypatch):
    """A notify_user message captured during a run that then hits a genuine
    round-level failure must be folded into the pushed result, not dropped."""
    notif = "REPORT: all endpoints healthy — https://example.com/status"

    job = {
        "id": "job-notify-fail",
        "name": "notify-fail",
        "prompt": "do the thing",
        "session_id": "sess-9",
        "schedule": {"type": "interval", "seconds": 1800},
        "enabled": True,
    }

    class _CrashingStream(FakeBedrockClient):
        """One tool_use round (a notify_user call), then a hard crash on the
        next round — simulating a genuine provider/transport failure."""

        def invoke_model_with_response_stream(self, modelId, contentType, accept, body):
            if self.requests:
                raise RuntimeError("simulated transport failure")
            return super().invoke_model_with_response_stream(modelId, contentType, accept, body)

    fake_stream = _CrashingStream(
        [
            [
                msg_start(),
                *tool_use_block("tu_1", "notify_user", {"message": notif}),
                *msg_end(stop_reason="tool_use"),
            ],
        ]
    )

    async def fake_route_tool(name, tool_input, **kwargs):
        return (f"Notification sent to user: {notif}", [{"notify_user": {"message": notif}}])

    recorded: dict = {}
    _run_cron_job(monkeypatch, job, fake_stream, fake_route_tool, recorded)

    assert recorded.get("status") == "failed"
    # The notify_user content survives...
    assert notif in recorded.get("text", "")
    # ...alongside the failure marker.
    assert "[FAILED]" in recorded.get("text", "")


def test_route_tool_receives_session_and_agent_stamp_without_leaking(monkeypatch):
    """Cron's tool dispatch now flows through the SAME shared
    execute_tool_batch every other unattended caller uses: the owning
    session id and the __agent__ unattended stamp are injected into the
    call, via a COPY so neither key leaks into the replayed transcript."""
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
        captured["session_id"] = kwargs.get("session_id")
        return ("done", [])

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
    recorded: dict = {}
    _run_cron_job(monkeypatch, job, fake_stream, fake_route_tool, recorded)

    # The executor saw the run's session id and the unattended stamp.
    assert captured.get("input", {}).get("__session_id__") == "sess-5"
    assert captured.get("input", {}).get("__agent__") is True
    assert captured.get("session_id") == "sess-5"
    # The run completed normally (end_turn on the second round).
    assert recorded.get("status") == "ok"
    # Neither internal key leaked into the assistant message replayed on
    # round 2 (bodies[1] carries the tool_use block's ORIGINAL input).
    assert len(fake_stream.requests) == 2
    body_2 = json.dumps(fake_stream.requests[1])
    assert "__session_id__" not in body_2
    assert "__agent__" not in body_2
