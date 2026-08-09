"""server/exec/headless.py's run_headless_turn: a Python-callable, non-
streaming primitive over the shared engine (server.chat.engine.runner.run_turn).

Mirrors the fixture patterns already established for the agent-runtime
migration: FakeBedrockClient (tests/golden_harness.py) scripts the REAL
AnthropicAdapter's Bedrock stream (run_headless_turn builds a real adapter
internally, exactly like server.agents.runtime._run_agent_loop does — there
is nothing to inject), and a fake `route_tool` stands in for tool execution
(tests/test_agent_engine_migration.py's own pattern).
"""

import asyncio
import json

from server.exec.headless import run_headless_turn
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block

CHAT_MODELS_CFG = {
    "chat_models": {"sonnet": "test-model"},
    "default_chat_model": "sonnet",
}


async def _collect(gen):
    return [event async for event in gen]


def _patch_common(monkeypatch, fake_stream, tools=None):
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    monkeypatch.setattr("server.chat.assemble_tool_pool", lambda *a, **k: tools or [])
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)

    from server.infrastructure import config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: dict(CHAT_MODELS_CFG))


# ── Event sequence: text, tool_call/tool_result, usage, done ────────────────


def test_headless_turn_scripted_event_sequence(monkeypatch):
    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "ws_grep", {"pattern": "x"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Found it."), *msg_end(stop_reason="end_turn")],
        ]
    )
    _patch_common(
        monkeypatch,
        fake_stream,
        tools=[{"name": "ws_grep", "description": "d", "input_schema": {"type": "object"}}],
    )

    async def fake_route_tool(tool_name, tool_input, **kw):
        return "matches found", []

    monkeypatch.setattr("server.tool_executor.route_tool", fake_route_tool)

    events = asyncio.run(
        _collect(
            run_headless_turn(
                "search the repo",
                model_key="sonnet",
                ephemeral=True,
                session_id="sess-headless-events",
            )
        )
    )

    types = [e["type"] for e in events]
    # Real frame order (see runner.py): the tool_call and its round's usage
    # frame both arrive BEFORE the tool actually executes (skill_result), then
    # round 2's text is flushed on ITS usage frame, then done.
    assert types == ["tool_call", "usage", "tool_result", "text", "usage", "done"]

    tool_call = events[0]
    assert tool_call["name"] == "ws_grep"
    assert tool_call["input"] == {"pattern": "x"}

    tool_result = events[2]
    assert tool_result["name"] == "ws_grep"
    assert tool_result["output"] == "matches found"
    assert tool_result["status"] == "ok"

    text_event = events[3]
    assert text_event["text"] == "Found it."

    final_usage = events[4]
    assert final_usage["input_tokens"] == 200  # 100 + 100, msg_start defaults
    assert final_usage["output_tokens"] == 100  # 50 + 50, msg_end defaults

    done = events[-1]
    assert done == {"type": "done", "status": "completed", "session_id": "sess-headless-events"}


# ── turn_scope_id isolation: a headless run must not perturb a real
# session's goal-store / auto-mode-breaker state ────────────────────────────


def _isolated_sessions_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    from server.infrastructure import sessions

    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sessions, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(sessions, "DB_PATH", str(storage / "sessions.db"))
    sessions._ensure_db()

    def _mk(session_id):
        with sessions._get_conn() as conn:
            conn.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, "t", "2026-01-01", "2026-01-01"),
            )
        return session_id

    return _mk


def test_headless_turn_scope_id_isolates_goal_store_and_auto_mode_breaker(monkeypatch, tmp_path):
    mk = _isolated_sessions_db(tmp_path, monkeypatch)

    from server.goals import store as goal_store
    from server.security import permissions as perms

    real_session = "real-chat-session"
    child_scope = f"exec:{real_session}"
    mk(real_session)
    mk(child_scope)

    goal_store.set_goal(real_session, "Ship the feature", set_at="t0")
    goal_store.record_block(real_session, "not_achieved", "keep going")
    goal_store.record_block(real_session, "not_achieved", "keep going")
    before = goal_store.get_goal(real_session)["state"]["consecutive_blocks"]
    assert before == 2

    perms._auto_mode_breaker.pop(real_session, None)
    for _ in range(3):
        perms.record_classifier_verdict(real_session, allowed=False)
    assert perms.is_auto_mode_tripped(real_session) is True

    fake_stream = FakeBedrockClient(
        [[msg_start(), *text_block("Headless task done."), *msg_end(stop_reason="end_turn")]]
    )
    _patch_common(monkeypatch, fake_stream)

    # session_id happens to equal a REAL, already-active chat session's own
    # id — proves run_headless_turn's own turn_scope_id (f"exec:{session_id}")
    # keeps its goal/breaker resets scoped away from that real session,
    # exactly as _run_agent_loop's turn_scope_id already does for subagents.
    events = asyncio.run(
        _collect(
            run_headless_turn(
                "do a subtask",
                model_key="sonnet",
                ephemeral=True,
                session_id=real_session,
            )
        )
    )
    assert events[-1]["status"] == "completed"

    after = goal_store.get_goal(real_session)["state"]["consecutive_blocks"]
    assert after == before == 2, (
        "a headless run must never perturb the real session's own goal-tracking "
        "state (turn_scope_id must key it, not the bare session_id)"
    )
    assert perms.is_auto_mode_tripped(real_session) is True, (
        "real session breaker must stay tripped"
    )

    # The child scope (what run_headless_turn actually keys state on) DID get
    # its own fresh-turn reset — proving turn_scope_id is genuinely read, not
    # just "nothing got reset at all".
    child_state = goal_store.get_goal(child_scope)["state"]
    assert child_state["consecutive_blocks"] == 0
    assert perms.is_auto_mode_tripped(child_scope) is False


# ── ephemeral session persistence ────────────────────────────────────────────


def test_headless_ephemeral_never_writes_sessions_db(monkeypatch, tmp_path):
    _isolated_sessions_db(tmp_path, monkeypatch)
    from server.infrastructure import sessions

    fake_stream = FakeBedrockClient(
        [[msg_start(), *text_block("Answer."), *msg_end(stop_reason="end_turn")]]
    )
    _patch_common(monkeypatch, fake_stream)

    events = asyncio.run(
        _collect(
            run_headless_turn(
                "a quick question",
                model_key="sonnet",
                ephemeral=True,
                session_id="sess-ephemeral-run",
            )
        )
    )
    assert events[-1]["status"] == "completed"
    assert sessions.get_session_meta("sess-ephemeral-run") is None, (
        "ephemeral=True must never create a sessions.db row"
    )


def test_headless_non_ephemeral_writes_sessions_db(monkeypatch, tmp_path):
    _isolated_sessions_db(tmp_path, monkeypatch)
    from server.infrastructure import sessions

    fake_stream = FakeBedrockClient(
        [[msg_start(), *text_block("The answer is 4."), *msg_end(stop_reason="end_turn")]]
    )
    _patch_common(monkeypatch, fake_stream)

    events = asyncio.run(
        _collect(
            run_headless_turn(
                "what is 2+2",
                model_key="sonnet",
                ephemeral=False,
                session_id="sess-persisted-run",
            )
        )
    )
    assert events[-1]["status"] == "completed"
    meta = sessions.get_session_meta("sess-persisted-run")
    assert meta is not None, "ephemeral=False must create/append to a sessions.db row"

    with sessions._get_conn() as conn:
        row = conn.execute(
            "SELECT chat_history FROM sessions WHERE id = ?", ("sess-persisted-run",)
        ).fetchone()
    history = json.loads(row["chat_history"])
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "what is 2+2"
    assert history[1]["role"] == "assistant"
    assert "The answer is 4." in history[1]["content"]


# ── output_schema: one extra structured-output call, validated ──────────────


class _FakeOldBedrock:
    """Stand-in for the OLD (server.agents.providers) Bedrock client that
    _distill_structured's adapter uses — same pattern as
    tests/test_agent_providers.py's test_run_agent_structured_schema_end_to_end."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []

    def invoke_model(self, **kwargs):
        self.requests.append(json.loads(kwargs["body"]))

        class _Body:
            def __init__(self, payload):
                self._payload = json.dumps(payload)

            def read(self):
                return self._payload

        return {"body": _Body(self._responses.pop(0))}


def test_headless_output_schema_yields_validated_structured_result(monkeypatch):
    fake_stream = FakeBedrockClient(
        [[msg_start(input_tokens=10), *text_block("4"), *msg_end(output_tokens=5)]]
    )
    _patch_common(monkeypatch, fake_stream)

    fake_old = _FakeOldBedrock(
        [
            {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "tool_use", "id": "s1", "name": "emit_result", "input": {"answer": 4}}
                ],
                "usage": {"input_tokens": 8, "output_tokens": 3},
            }
        ]
    )
    monkeypatch.setattr("server.chat._get_bedrock_client", lambda: fake_old)

    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    }
    events = asyncio.run(
        _collect(
            run_headless_turn(
                "what is 2+2",
                model_key="sonnet",
                ephemeral=True,
                session_id="sess-structured",
                output_schema=schema,
            )
        )
    )
    structured_events = [e for e in events if e["type"] == "structured"]
    assert len(structured_events) == 1
    assert structured_events[0]["output"] == {"answer": 4}

    # usage aggregated across BOTH calls (main turn + distillation): the final
    # usage event must reflect the distillation call's own usage, not just the
    # main turn's.
    usage_events = [e for e in events if e["type"] == "usage"]
    assert usage_events[-1]["input_tokens"] == 10 + 8
    assert usage_events[-1]["output_tokens"] == 5 + 3

    assert events[-1]["status"] == "completed"


# ── unattended=True on the built context: a [WS_APPROVAL] tool executes
# inline instead of pausing ──────────────────────────────────────────────────


def test_headless_context_is_unattended_and_ws_approval_executes_inline(monkeypatch):
    from server.approval import registry as approval_registry
    from server.approval.spec import ApprovalOutcome, ApprovalSpec
    from server.chat.engine.pause import paused_sessions

    executed_payloads: list[dict] = []

    async def fake_exec(payload):
        executed_payloads.append(dict(payload))
        return ApprovalOutcome(ok=True, output="[OK] wrote it")

    approval_registry.register(
        "test_headless_write",
        ApprovalSpec(category="test", preview="text", summary="t", executor=fake_exec),
    )

    dispatched_inputs: list[dict] = []

    async def fake_route_tool(tool_name, tool_input, **kw):
        dispatched_inputs.append(dict(tool_input))
        sentinel = json.dumps({"action": "test_headless_write", "path": "x.txt"})
        return f"[WS_APPROVAL]{sentinel}", []

    monkeypatch.setattr("server.tool_executor.route_tool", fake_route_tool)

    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "ws_write_file", {"path": "x.txt", "content": "hi"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Wrote the file."), *msg_end(stop_reason="end_turn")],
        ]
    )
    _patch_common(
        monkeypatch,
        fake_stream,
        tools=[{"name": "ws_write_file", "description": "d", "input_schema": {"type": "object"}}],
    )

    # Direct regression check: spy on the real run_turn to capture the built
    # TurnContext's own unattended/turn_scope_id fields, rather than only
    # inferring them behaviorally.
    import server.chat.engine.runner as runner_mod

    real_run_turn = runner_mod.run_turn
    captured: dict = {}

    async def _spy_run_turn(ctx):
        captured["unattended"] = ctx.unattended
        captured["turn_scope_id"] = ctx.turn_scope_id
        async for chunk in real_run_turn(ctx):
            yield chunk

    monkeypatch.setattr(runner_mod, "run_turn", _spy_run_turn)

    paused_sessions.pop("sess-headless-approval", None)
    events = asyncio.run(
        _collect(
            run_headless_turn(
                "write the file",
                model_key="sonnet",
                ephemeral=True,
                session_id="sess-headless-approval",
            )
        )
    )

    assert captured["unattended"] is True
    assert captured["turn_scope_id"] == "exec:sess-headless-approval"

    # The dispatched call carries the __agent__ stamp (execute_tool_batch).
    assert dispatched_inputs[0]["__agent__"] is True
    # The approval executed inline and unconditionally — no pause.
    assert executed_payloads[0]["__agent__"] is True
    assert executed_payloads[0]["path"] == "x.txt"
    assert "sess-headless-approval" not in paused_sessions

    assert any(e["type"] == "text" and "Wrote the file." in e["text"] for e in events)
    assert events[-1]["status"] == "completed"


# ── pre-flight validation: unknown model_key / all-local config / explicit
# local model_key all fail cleanly with an error + done(failed), no turn runs ─


def test_headless_unknown_model_key_fails_cleanly(monkeypatch):
    from server.infrastructure import config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: dict(CHAT_MODELS_CFG))

    events = asyncio.run(
        _collect(run_headless_turn("hi", model_key="does-not-exist", ephemeral=True))
    )
    assert events[0]["type"] == "error"
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "failed"


def test_headless_refuses_explicit_local_model(monkeypatch):
    from server.infrastructure import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"chat_models": {"local_gemma": "local:gemma-4-12b"}, "default_chat_model": None},
    )

    events = asyncio.run(_collect(run_headless_turn("hi", model_key="local_gemma", ephemeral=True)))
    assert events[0]["type"] == "error"
    assert "on-device" in events[0]["message"].lower()
    assert events[-1] == {
        "type": "done",
        "status": "failed",
        "session_id": events[-1]["session_id"],
    }
