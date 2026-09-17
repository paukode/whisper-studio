"""Voice tool set: ask_assistant drives an attended headless turn, parks pauses
(approval / question / folder) for resolve_request, which executes the decision
and resumes the same run. The headless runner is faked; no model, no network."""

import asyncio

import pytest

from server.voice import tools as voice_tools
from server.voice.tools import ToolContext, _classify_decision, run_tool


def _ctx(events, pending=None):
    async def emit(ev):
        events.append(ev)

    return ToolContext(
        session_id="s1",
        model_key="opus5.0",
        emit=emit,
        request_end=lambda: None,
        pending=pending if pending is not None else {},
        session_approvals={"cli": "ask"},
    )


def _fake_headless(scripts, calls):
    """scripts: list of event lists, consumed per run_headless_turn call."""

    async def fake(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        for ev in scripts.pop(0):
            yield ev

    return fake


def test_classify_decision():
    assert _classify_decision("Yes, go ahead.") == "approve"
    assert _classify_decision("approve") == "approve"
    assert _classify_decision("okay") == "approve"
    assert _classify_decision("sure, do it") == "approve"
    assert _classify_decision("No thanks") == "deny"
    assert _classify_decision("deny it") == "deny"
    assert _classify_decision("maybe later") is None
    assert _classify_decision("") is None
    # A refusal anywhere wins: safer to ask again than to run something unwanted.
    assert _classify_decision("okay, no") == "deny"
    assert _classify_decision("yes but don't run it") == "deny"
    assert _classify_decision("not now") == "deny"
    assert _classify_decision("nope") == "deny"
    # Words that merely contain an approval word are not approvals.
    assert _classify_decision("nokia") is None
    assert _classify_decision("yesterday") is None


def test_ask_assistant_returns_answer_and_forwards_steps(monkeypatch):
    calls, events = [], []
    monkeypatch.setattr(
        "server.exec.headless.run_headless_turn",
        _fake_headless(
            [
                [
                    {"type": "tool_call", "name": "git_branch_list", "input": {}},
                    {
                        "type": "tool_result",
                        "name": "git_branch_list",
                        "output": "main\\nsonic",
                        "status": "ok",
                    },
                    {"type": "text", "text": "Branches: main, sonic."},
                    {"type": "done", "status": "completed", "session_id": "x"},
                ]
            ],
            calls,
        ),
    )
    out = asyncio.run(run_tool("ask_assistant", {"request": "list branches"}, _ctx(events)))
    assert out == "Branches: main, sonic."
    assert calls[0]["attended"] is True and calls[0]["session_approvals"] == {"cli": "ask"}
    assert calls[0]["model_key"] == "opus5.0" and calls[0]["ephemeral"] is True
    steps = [(e["name"], e["status"]) for e in events if e["type"] == "assistant_step"]
    assert steps == [("git_branch_list", "running"), ("git_branch_list", "ok")]


def test_ask_assistant_parks_an_approval_and_resolve_resumes_it(monkeypatch):
    calls, events = [], []
    pending = {}
    monkeypatch.setattr(
        "server.exec.headless.run_headless_turn",
        _fake_headless(
            [
                [
                    {"type": "text", "text": "I need to run pytest."},
                    {
                        "type": "approval_request",
                        "tool_use_id": "tu-7",
                        "action": "command",
                        "category": "cli",
                        "summary": "Run: pytest -q",
                        "payload": {"command": "pytest -q"},
                        "preview": "command",
                        "risk_hint": "medium",
                    },
                    {"type": "done", "status": "paused", "session_id": "x"},
                ],
                [
                    {"type": "text", "text": "All 214 tests passed."},
                    {"type": "done", "status": "completed", "session_id": "x"},
                ],
            ],
            calls,
        ),
    )

    class _Spec:
        category = "cli"

        async def executor(self, payload):
            from server.approval.spec import ApprovalOutcome

            return ApprovalOutcome(ok=True, output="ran " + payload["command"])

    monkeypatch.setattr(
        "server.approval.registry.get", lambda action: _Spec() if action == "command" else None
    )

    ctx = _ctx(events, pending)
    out = asyncio.run(run_tool("ask_assistant", {"request": "run the tests"}, ctx))
    assert "needs the user's approval to Run: pytest -q" in out
    assert "resolve_request" in out
    assert pending["type"] == "approval_request" and pending["tool_use_id"] == "tu-7"
    req = [e for e in events if e["type"] == "assistant_request"]
    assert req and req[0]["summary"] == "Run: pytest -q" and req[0]["detail"] == "pytest -q"

    # A second ask while one is pending is refused.
    again = asyncio.run(run_tool("ask_assistant", {"request": "something else"}, ctx))
    assert again.startswith("Error:") and "resolve_request" in again

    out2 = asyncio.run(run_tool("resolve_request", {"decision": "yes please"}, ctx))
    assert out2 == "All 214 tests passed."
    assert pending == {}
    resume = calls[1]
    assert resume["scope_id"] == calls[0]["scope_id"]  # same run is resumed
    answers = resume["resume"]["answers"]
    assert answers[0]["tool_use_id"] == "tu-7"
    assert answers[0]["content"].startswith(
        "[User approved] command: pytest -q. The action succeeded."
    )
    assert "ran pytest -q" in answers[0]["content"]
    assert any(e["type"] == "assistant_request_resolved" for e in events)


def test_resolve_deny_does_not_execute(monkeypatch):
    calls, events = [], []
    pending = {
        "type": "approval_request",
        "tool_use_id": "tu-1",
        "action": "ws_delete_file",
        "payload": {"path": "/tmp/x"},
        "summary": "Delete /tmp/x",
        "run_id": "voice-s1-abc",
    }
    executed = []

    class _Spec:
        category = "delete"

        async def executor(self, payload):
            executed.append(payload)

    monkeypatch.setattr("server.approval.registry.get", lambda action: _Spec())
    monkeypatch.setattr(
        "server.exec.headless.run_headless_turn",
        _fake_headless(
            [
                [
                    {"type": "text", "text": "Okay, left it alone."},
                    {"type": "done", "status": "completed", "session_id": "x"},
                ]
            ],
            calls,
        ),
    )
    out = asyncio.run(run_tool("resolve_request", {"decision": "no"}, _ctx(events, pending)))
    assert out == "Okay, left it alone."
    assert executed == []
    assert calls[0]["resume"]["answers"][0]["content"].startswith(
        "[User denied] ws_delete_file: /tmp/x."
    )
    assert calls[0]["scope_id"] == "voice-s1-abc" and calls[0]["session_id"] == "s1"
    assert calls[0]["event_channel"] == "voice:voice-s1-abc"


def test_resolve_question_and_missing_pending(monkeypatch):
    calls, events = [], []
    pending = {
        "type": "user_question",
        "tool_use_id": "q1",
        "question": "Which env?",
        "options": ["dev", "prod"],
        "run_id": "r9",
    }
    monkeypatch.setattr(
        "server.exec.headless.run_headless_turn",
        _fake_headless(
            [
                [
                    {"type": "text", "text": "Deploying to dev."},
                    {"type": "done", "status": "completed", "session_id": "x"},
                ]
            ],
            calls,
        ),
    )
    out = asyncio.run(run_tool("resolve_request", {"decision": "dev"}, _ctx(events, pending)))
    assert out == "Deploying to dev."
    assert calls[0]["resume"]["answers"] == [{"tool_use_id": "q1", "content": "dev"}]
    nothing = asyncio.run(run_tool("resolve_request", {"decision": "yes"}, _ctx(events, {})))
    assert nothing.startswith("Error: there is no pending request")
    vague = asyncio.run(
        run_tool(
            "resolve_request",
            {"decision": "hmm"},
            _ctx(
                events,
                {
                    "type": "approval_request",
                    "tool_use_id": "t",
                    "action": "command",
                    "payload": {},
                    "run_id": "r",
                },
            ),
        )
    )
    assert vague.startswith("Error: for an approval")


def test_ask_assistant_deadline_keeps_partial_output(monkeypatch):
    """The safety cap never discards what the run produced."""
    monkeypatch.setattr(voice_tools, "ASSISTANT_DEADLINE_S", 0.05)

    async def slow(prompt, **kw):
        yield {"type": "text", "text": "Starting."}
        await asyncio.sleep(1)
        yield {"type": "done", "status": "completed", "session_id": "x"}

    monkeypatch.setattr("server.exec.headless.run_headless_turn", slow)
    events = []
    out = asyncio.run(run_tool("ask_assistant", {"request": "x"}, _ctx(events)))
    assert out.startswith("The assistant hit the time limit") and "Starting." in out
    answers = [e for e in events if e["type"] == "assistant_answer"]
    assert len(answers) == 1 and "Starting." in answers[0]["output"]


@pytest.mark.parametrize("name", ["control_recording", "end_conversation"])
def test_other_tools_still_answer(name):
    events = []
    out = asyncio.run(run_tool(name, {"action": "start"}, _ctx(events)))
    assert isinstance(out, str) and out


def test_duplicate_ask_and_premature_resolve_are_refused_while_a_run_is_in_flight(monkeypatch):
    """Sonic tends to relay the same request twice and to call resolve before the
    assistant paused; both are refused with guidance. A different request is
    allowed to run alongside."""
    gate = asyncio.Event()
    calls = []

    async def slow(prompt, **kw):
        calls.append(prompt)
        await gate.wait()
        yield {"type": "text", "text": f"Done: {prompt}"}
        yield {"type": "done", "status": "completed", "session_id": "x"}

    monkeypatch.setattr("server.exec.headless.run_headless_turn", slow)
    events = []
    ctx = _ctx(events)

    async def scenario():
        first = asyncio.create_task(run_tool("ask_assistant", {"request": "list branches"}, ctx))
        await asyncio.sleep(0.05)
        assert [i["request"] for i in ctx.runs.values()] == ["list branches"]
        dup = await run_tool("ask_assistant", {"request": "list the branches"}, ctx)
        assert dup.startswith("Error: the assistant is already working on that (list branches)")
        early = await run_tool("resolve_request", {"decision": "yes"}, ctx)
        assert early.startswith("Error: nothing to resolve yet")
        second = asyncio.create_task(
            run_tool("ask_assistant", {"request": "check the readme"}, ctx)
        )
        await asyncio.sleep(0.05)
        assert len(ctx.runs) == 2
        gate.set()
        return await first, await second

    assert asyncio.run(scenario()) == ("Done: list branches", "Done: check the readme")
    assert calls == ["list branches", "check the readme"]
    assert ctx.runs == {}
    assert [e["output"] for e in events if e["type"] == "assistant_answer"] == [
        "Done: list branches",
        "Done: check the readme",
    ]


def test_long_run_detaches_and_delivers_its_answer_later(monkeypatch):
    """Past ASK_WAIT_S ask_assistant returns "Working on it" and the run keeps
    going; its answer reaches the browser (assistant_answer, with the run's
    steps tagged by run_id) and Sonic (deliver_late) when it lands."""
    monkeypatch.setattr(voice_tools, "ASK_WAIT_S", 0.05)
    gate = asyncio.Event()

    async def slow(prompt, **kw):
        yield {"type": "tool_call", "name": "spawn_agent", "input": {"task": "bugs"}}
        await gate.wait()
        yield {"type": "tool_result", "name": "spawn_agent", "output": "3 findings", "status": "ok"}
        yield {"type": "text", "text": "Found three bugs."}
        yield {"type": "done", "status": "completed", "session_id": "x"}

    monkeypatch.setattr("server.exec.headless.run_headless_turn", slow)
    events, late, tasks = [], [], []
    ctx = _ctx(events)

    async def deliver(text):
        late.append(text)

    ctx.deliver_late = deliver
    ctx.register_task = tasks.append

    async def scenario():
        out = await run_tool("ask_assistant", {"request": "check for bugs"}, ctx)
        assert out.startswith("Working on it: check for bugs")
        assert len(ctx.runs) == 1 and next(iter(ctx.runs.values()))["detached"] is True
        assert late == []
        # Meanwhile a quick request is served normally.
        quick = asyncio.create_task(run_tool("ask_assistant", {"request": "read the readme"}, ctx))
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.gather(*tasks)
        return await quick

    assert asyncio.run(scenario()) == "Found three bugs."
    # Only the detached run is delivered late; the quick one answered directly.
    assert len(late) == 1
    assert late[0].startswith("[Result of the request 'check for bugs'.")
    assert "Do not call any tool" in late[0] and late[0].endswith(":] Found three bugs.")
    answers = [e for e in events if e["type"] == "assistant_answer"]
    assert [a["request"] for a in answers] == ["check for bugs", "read the readme"]
    steps = [e for e in events if e["type"] == "assistant_step"]
    assert {st["run_id"] for st in steps} == {a["run_id"] for a in answers}
    assert len({a["run_id"] for a in answers}) == 2
    assert ctx.runs == {}


def test_detached_run_pause_is_announced_and_queued_behind_a_current_one(monkeypatch):
    monkeypatch.setattr(voice_tools, "ASK_WAIT_S", 0.05)
    gate = asyncio.Event()

    async def slow(prompt, **kw):
        await gate.wait()
        yield {
            "type": "approval_request",
            "tool_use_id": "t2",
            "action": "command",
            "summary": "run pytest",
            "payload": {"command": "pytest"},
        }
        yield {"type": "done", "status": "paused", "session_id": "x"}

    monkeypatch.setattr("server.exec.headless.run_headless_turn", slow)
    events, late, tasks = [], [], []
    ctx = _ctx(events)

    async def deliver(text):
        late.append(text)

    ctx.deliver_late = deliver
    ctx.register_task = tasks.append

    async def scenario():
        out = await run_tool("ask_assistant", {"request": "run the tests"}, ctx)
        assert out.startswith("Working on it")
        # Another request already waits on the user when this one pauses.
        ctx.pending.update({"type": "user_question", "tool_use_id": "q1", "run_id": "r0"})
        gate.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert ctx.pending["tool_use_id"] == "q1"
    assert [q["tool_use_id"] for q in ctx.pending_queue] == ["t2"]
    # Sonic is told the earlier request comes first, and is NOT asked to collect
    # a decision for the queued one (that answer would land on the current one).
    assert len(late) == 1 and late[0].startswith("[About the request 'run the tests':]")
    assert "earlier request must be answered first" in late[0]
    assert "run pytest" not in late[0]
    # No request card yet for the queued one: the current card is still up.
    assert [e for e in events if e["type"] == "assistant_request"] == []


def test_concurrent_resolves_execute_the_approval_once(monkeypatch):
    """A tap on the card and a spoken yes can land together: the action runs
    exactly once and only one resume turn is launched."""
    calls, events, executed = [], [], []

    class Outcome:
        ok, output, error = True, "did it", None

    class Spec:
        category = "write"

        async def executor(self, payload):
            executed.append(payload)
            await asyncio.sleep(0.05)
            return Outcome()

    monkeypatch.setattr("server.approval.registry.get", lambda action: Spec())
    monkeypatch.setattr(
        "server.exec.headless.run_headless_turn",
        _fake_headless(
            [[{"type": "text", "text": "Done."}, {"type": "done", "status": "completed"}]] * 2,
            calls,
        ),
    )
    pending = {
        "type": "approval_request",
        "tool_use_id": "t1",
        "run_id": "voice-s1-abc",
        "action": "ws_write_file",
        "payload": {"path": "/tmp/x"},
        "summary": "write x",
    }
    ctx = _ctx(events, pending)

    async def both():
        return await asyncio.gather(
            run_tool("resolve_request", {"decision": "approve"}, ctx),
            run_tool("resolve_request", {"decision": "yes"}, ctx),
        )

    first, second = asyncio.run(both())
    assert first == "Done."
    assert second.startswith("Error: there is no pending request")
    assert len(executed) == 1 and len(calls) == 1


def test_cancelled_run_reports_a_stopped_answer(monkeypatch):
    """Stopping a background run keeps its trace on screen: the browser gets an
    assistant_answer with status stopped, like a stopped chat turn."""
    monkeypatch.setattr(voice_tools, "ASK_WAIT_S", 0.05)

    async def forever(prompt, **kw):
        yield {"type": "tool_call", "name": "spawn_agent", "input": {"task": "x"}}
        await asyncio.sleep(30)
        yield {"type": "done", "status": "completed", "session_id": "x"}

    monkeypatch.setattr("server.exec.headless.run_headless_turn", forever)
    events, tasks = [], []
    ctx = _ctx(events)
    ctx.register_task = tasks.append

    async def scenario():
        out = await run_tool("ask_assistant", {"request": "review"}, ctx)
        assert out.startswith("Working on it")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
    stopped = [e for e in events if e["type"] == "assistant_answer"]
    assert len(stopped) == 1 and stopped[0]["status"] == "stopped"
    assert stopped[0]["output"] == "(Stopped)" and stopped[0]["request"] == "review"
    assert ctx.runs == {}
