"""Phase 0 engine extension points: unattended turns, turn_scope_id, and
deadline enforcement.

These are the pieces server/agents/runtime.py's inner loop will be ported
onto (Phase 1). Bare TurnContext + a scripted adapter, following the pattern
already used by tests/test_engine_local.py — no HTTP route, no real Bedrock.
"""

import asyncio
import json

import pytest

from server.approval import registry as approval_registry
from server.approval.spec import ApprovalOutcome, ApprovalSpec
from server.chat.engine.events import RoundResult, TextDelta, ToolCall, ToolCallStart, Usage
from server.chat.engine.pause import paused_sessions
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn


class ScriptedAdapter:
    """A minimal adapter: one scripted round's worth of (events, RoundResult)
    per call, in order. Records every call's (messages, tools) for assertions."""

    provider = "test"

    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls: list[dict] = []

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls.append(
            {
                "messages": [dict(m) for m in messages],
                "tools": tools,
                "round_num": round_num,
                "is_last_round": is_last_round,
            }
        )
        if not self._rounds:
            raise RuntimeError("ScriptedAdapter: no scripted rounds left")
        events = self._rounds.pop(0)
        for ev in events:
            yield ev


def _tool_use_round(tool_id, name, tool_input):
    return [
        ToolCallStart(name=name),
        ToolCall(id=tool_id, name=name, input=tool_input),
        RoundResult(
            stop_reason="tool_use",
            content=[{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
            usage=Usage(),
        ),
    ]


def _text_round(text, stop_reason="end_turn"):
    return [
        TextDelta(text=text),
        RoundResult(
            stop_reason=stop_reason, content=[{"type": "text", "text": text}], usage=Usage()
        ),
    ]


def _base_ctx(**overrides) -> TurnContext:
    defaults = dict(
        session_id="sess-base",
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": "hi"}],
        adapter=None,
        policy=TurnPolicy(max_rounds=10, completion_gate=False),
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
    )
    defaults.update(overrides)
    return TurnContext(**defaults)


async def _drain(ctx) -> str:
    return "".join([c async for c in run_turn(ctx)])


# ── unattended=True: WS_APPROVAL executes inline, stamped __agent__ ─────────


def test_unattended_ws_approval_executes_inline_with_agent_stamp(monkeypatch):
    executed_payloads: list[dict] = []

    async def fake_exec(payload):
        executed_payloads.append(dict(payload))
        return ApprovalOutcome(ok=True, output="[OK] wrote it")

    approval_registry.register(
        "test_unattended_write",
        ApprovalSpec(category="test", preview="text", summary="t", executor=fake_exec),
    )

    dispatched_inputs: list[dict] = []

    async def fake_route_tool(tool_name, tool_input, **kw):
        dispatched_inputs.append(dict(tool_input))
        sentinel = json.dumps({"action": "test_unattended_write", "path": "x.txt"})
        return f"[WS_APPROVAL]{sentinel}", []

    # tool_executor.py imports route_tool at module level (`from
    # server.tool_router import ... route_tool`) — patch ITS binding, not
    # tool_router's, since that's the reference _run_one actually calls.
    monkeypatch.setattr("server.tool_executor.route_tool", fake_route_tool)

    adapter = ScriptedAdapter(
        [
            _tool_use_round("t1", "ws_write_file", {"path": "x.txt", "content": "hi"}),
            _text_round("Wrote the file."),
        ]
    )
    paused_sessions.pop("sess-unattended-approval", None)
    ctx = _base_ctx(
        session_id="sess-unattended-approval",
        adapter=adapter,
        unattended=True,
    )
    blob = asyncio.run(_drain(ctx))

    # The dispatched call carries the __agent__ stamp (execute_tool_batch).
    assert dispatched_inputs[0]["__agent__"] is True
    # The approval executed inline and unconditionally (agent=True) — no
    # category/classifier gate consulted.
    assert executed_payloads[0]["__agent__"] is True
    assert executed_payloads[0]["path"] == "x.txt"
    # No approval_request frame, no pause.
    assert "approval_request" not in blob
    assert "sess-unattended-approval" not in paused_sessions
    assert "Wrote the file." in blob
    assert blob.strip().endswith("data: [DONE]")
    # Both scripted rounds ran — the turn continued past the auto-approved call.
    assert len(adapter.calls) == 2


# ── unattended=True: ask_user_question resolves to a refusal, not a pause ───


def test_unattended_ask_user_question_refuses_instead_of_pausing():
    adapter = ScriptedAdapter(
        [
            _tool_use_round("q1", "ask_user_question", {"question": "Which color?"}),
            _text_round("Went with blue since you're unavailable."),
        ]
    )
    paused_sessions.pop("sess-unattended-ask", None)
    ctx = _base_ctx(
        session_id="sess-unattended-ask",
        adapter=adapter,
        unattended=True,
    )
    blob = asyncio.run(_drain(ctx))

    # Not paused — the turn continued to a second round.
    assert "sess-unattended-ask" not in paused_sessions
    assert len(adapter.calls) == 2
    # The refusal text reached the model as the tool_result for round 2.
    round2_messages = adapter.calls[1]["messages"]
    joined = json.dumps(round2_messages)
    assert "cannot ask the user a question unattended" in joined
    assert "Went with blue" in blob
    assert blob.strip().endswith("data: [DONE]")


# ── unattended=False (default): existing pause behavior is unchanged ────────


def test_attended_ask_user_question_still_pauses_as_today():
    adapter = ScriptedAdapter(
        [
            _tool_use_round("q1", "ask_user_question", {"question": "Which color?"}),
            _text_round("unreachable — the turn should have paused first"),
        ]
    )
    paused_sessions.pop("sess-attended-ask", None)
    ctx = _base_ctx(
        session_id="sess-attended-ask",
        adapter=adapter,
        unattended=False,
    )
    blob = asyncio.run(_drain(ctx))

    assert "sess-attended-ask" in paused_sessions
    stashed = paused_sessions.pop("sess-attended-ask")
    assert stashed["messages"][-1]["content"][-1]["type"] == "tool_use"
    assert '"user_question"' in blob
    assert blob.strip().endswith("data: [DONE]")
    # Only ONE round ran — the turn paused instead of continuing.
    assert len(adapter.calls) == 1


# ── turn_scope_id: the real session's goal/breaker state is untouched ───────


@pytest.fixture
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


def test_turn_scope_id_isolates_goal_store_and_auto_mode_breaker(_isolated_sessions_db):
    """The single most important new test: proves the real fix for the real
    latent bug. execute_spawn_agent already calls run_agent(session_id=<the
    PARENT chat session's own id>, ...) — if a subagent's inner run_turn call
    used that raw session_id, it would reset the PARENT's in-flight goal
    tracking and auto-mode breaker mid-turn. turn_scope_id must be the ONLY
    thing that gets reset; the real session_id must come out exactly as it
    went in.
    """
    from server.goals import store as goal_store
    from server.security import permissions as perms

    real_session = "real-parent-session"
    child_scope = "agent:child-1"
    _isolated_sessions_db(real_session)
    _isolated_sessions_db(child_scope)

    # Seed BOTH the real session and the child scope with an active goal and
    # a couple of consecutive blocks, so "reset" is observable either way.
    goal_store.set_goal(real_session, "Parent goal", set_at="t0")
    goal_store.record_block(real_session, "not_achieved", "keep going")
    goal_store.record_block(real_session, "not_achieved", "keep going")
    goal_store.set_goal(child_scope, "Child task goal", set_at="t0")
    goal_store.record_block(child_scope, "not_achieved", "keep going")
    goal_store.record_block(child_scope, "not_achieved", "keep going")

    # Seed BOTH auto-mode breakers as already-tripped.
    perms._auto_mode_breaker.pop(real_session, None)
    perms._auto_mode_breaker.pop(child_scope, None)
    for _ in range(3):
        perms.record_classifier_verdict(real_session, allowed=False)
        perms.record_classifier_verdict(child_scope, allowed=False)
    assert perms.is_auto_mode_tripped(real_session) is True
    assert perms.is_auto_mode_tripped(child_scope) is True

    adapter = ScriptedAdapter([_text_round("done")])
    ctx = _base_ctx(
        session_id=real_session,
        adapter=adapter,
        turn_scope_id=child_scope,
        is_new_turn=True,
    )
    asyncio.run(_drain(ctx))

    # The REAL session's goal state and breaker are byte-identical to before —
    # a subagent's inner turn must never perturb the parent's live turn state.
    real_state = goal_store.get_goal(real_session)["state"]
    assert real_state["consecutive_blocks"] == 2, "parent goal state must be untouched"
    assert perms.is_auto_mode_tripped(real_session) is True, "parent breaker must stay tripped"

    # The CHILD scope genuinely got the new-turn reset — proving turn_scope_id
    # is actually being read, not just "nothing got reset at all".
    child_state = goal_store.get_goal(child_scope)["state"]
    assert child_state["consecutive_blocks"] == 0, "child scope's own state must reset"
    assert perms.is_auto_mode_tripped(child_scope) is False, "child scope's breaker must reset"


# ── deadline enforcement: a deadline-hit round is genuinely terminal ────────


def test_deadline_hit_forces_a_terminal_no_tools_round():
    # The adapter simulates real provider behavior: it can only "call a tool"
    # when tools are actually offered to it. A short deadline must force an
    # empty tool list on round 0 (before any real work happens), so the turn
    # finalizes immediately instead of looping toward max_rounds.
    class DeadlineAwareAdapter:
        provider = "test"

        def __init__(self):
            self.calls: list[dict] = []

        async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
            self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
            if tools:
                yield ToolCallStart(name="some_tool")
                yield ToolCall(id="t1", name="some_tool", input={})
                yield RoundResult(
                    stop_reason="tool_use",
                    content=[{"type": "tool_use", "id": "t1", "name": "some_tool", "input": {}}],
                    usage=Usage(),
                )
            else:
                yield TextDelta(text="Final answer given the time limit.")
                yield RoundResult(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "Final answer given the time limit."}],
                    usage=Usage(),
                )

    adapter = DeadlineAwareAdapter()
    ctx = _base_ctx(
        session_id="sess-deadline",
        adapter=adapter,
        policy=TurnPolicy(max_rounds=10, deadline_seconds=0.0001, completion_gate=False),
    )
    blob = asyncio.run(_drain(ctx))

    # Exactly one round ran — the turn did not loop toward max_rounds=10.
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["tools"] == []
    # The finalize-now reminder was injected into the round's own request.
    assert "Time is up for this turn" in json.dumps(adapter.calls[0]["messages"])
    assert "Final answer given the time limit." in blob
    assert "(Reached maximum tool rounds)" not in blob
    assert blob.strip().endswith("data: [DONE]")
