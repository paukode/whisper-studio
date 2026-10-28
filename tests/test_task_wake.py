"""A finished background command comes back to the chat that started it.

A field session (a signed release build, 1 h 43 min) checked its own build
log 53 times: the model had no other way to learn that a background command
had finished. Now (server/tasks/wake.py):

- a command a chat turn started reports back when it finishes: a task_report
  row the model reads as data, folded into a running turn through the mid-turn
  inbox, or a task_wake frame that asks the app to start a turn;
- an agent's command, the user's own command and a command stopped on request
  wake nobody; one the runtime cap stopped does;
- a wake turn's evidence counts the command as one of its calls, run since it
  started, so a reply may rest on what it did;
- the background cap fits a release build.
"""

import time

import pytest

from server.chat import stream_slot
from server.chat.engine import midturn_inbox
from server.chat.engine.runner import _midturn_text
from server.infrastructure.sessions import visible_chat_history
from server.infrastructure.sessions_routes import _session_event_frame
from server.tasks import registry, shell
from server.tasks import wake as task_wake

SID = "s-wake"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))
    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()
    midturn_inbox.clear(SID)
    yield
    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()
    midturn_inbox.clear(SID)


@pytest.fixture
def out(monkeypatch):
    """What reached the session: rows persisted, frames published."""
    rows: list[tuple[str, dict]] = []
    frames: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        task_wake,
        "emit_session_event",
        lambda sid, *, role, payload_key, payload: rows.append((role, payload)),
    )
    from server.agents.event_bus import event_bus

    monkeypatch.setattr(event_bus, "publish", lambda sid, ev: frames.append((sid, ev)))
    return rows, frames


def _task(**over) -> dict:
    task = {
        "task_id": "t1",
        "kind": "shell",
        "session_id": SID,
        "title": "make release",
        "command": "make release",
        "status": "completed",
        "exit_code": 0,
        "result_text": "built dist/App.dmg",
        "created_at": "2026-10-28T15:20:05Z",
        "finished_at": "2026-10-28T17:03:22Z",
        "meta": {"cwd": "/w", task_wake.WAKE_META_KEY: True},
    }
    task.update(over)
    return task


def _wake_frames(frames):
    return [ev["taskWake"] for _, ev in frames if ev.get("type") == "task_wake"]


# ── who reports back ────────────────────────────────────────────────────────


def test_an_idle_chat_gets_the_row_and_a_wake(out):
    rows, frames = out
    assert task_wake.report_finished(_task(), stopped_on_request=False) == "wake"
    assert [r for r, _ in rows] == ["task_report"]
    payload = rows[0][1]
    assert payload["command"] == "make release" and payload["exit_code"] == 0
    assert payload["duration_ms"] == (1 * 3600 + 43 * 60 + 17) * 1000
    assert _wake_frames(frames) == [{"task_ids": ["t1"]}]


def test_a_running_turn_reads_it_at_its_next_round(out):
    rows, frames = out
    stream_slot.active_streams[SID] = time.monotonic()
    assert task_wake.report_finished(_task(), stopped_on_request=False) == "live"
    assert [r for r, _ in rows] == ["task_report"]
    assert _wake_frames(frames) == []
    (entry,) = midturn_inbox.drain(SID)
    assert entry.kind == midturn_inbox.TASK_REPORT
    assert "make release" in entry.text and entry.payload["task_id"] == "t1"


def test_a_turn_that_is_ending_hands_it_to_a_wake(out):
    _, frames = out
    stream_slot.active_streams[SID] = time.monotonic()
    midturn_inbox.close_and_take(SID)
    assert task_wake.report_finished(_task(), stopped_on_request=False) == "wake"
    assert _wake_frames(frames) == [{"task_ids": ["t1"]}]


def test_a_result_the_turn_never_read_asks_for_a_wake(out):
    _, frames = out
    payload = task_wake.report_payload(_task())
    assert midturn_inbox.push(
        SID, task_wake.report_body(payload), kind=midturn_inbox.TASK_REPORT, payload=payload
    )
    assert midturn_inbox.close_and_announce(SID) == []
    assert _wake_frames(frames) == [{"task_ids": ["t1"]}]


@pytest.mark.parametrize(
    "task, stopped",
    [
        (_task(meta={"cwd": "/w"}), False),  # an agent's or the user's command
        (_task(session_id=""), False),
        (_task(kind="agent"), False),
        (_task(status="stopped", exit_code=None), True),  # Stop or task_cancel
    ],
)
def test_who_wakes_nobody(out, task, stopped):
    rows, frames = out
    assert task_wake.report_finished(task, stopped_on_request=stopped).startswith("skipped")
    assert rows == [] and frames == []


def test_the_runtime_cap_still_reports_back(out):
    rows, _ = out
    capped = _task(status="stopped", exit_code=-9, result_text="[stopped: exceeded the cap]")
    assert task_wake.report_finished(capped, stopped_on_request=False) == "wake"
    assert rows[0][1]["status"] == "stopped"


# ── the shell runner and the command tools set it up ───────────────────────


def _wait_finished(task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = registry.get_task(task_id)
        if task and task["status"] != "running":
            return task
        time.sleep(0.05)
    raise AssertionError("the task never finished")


def test_a_chat_command_reports_back_when_it_finishes(tmp_path, monkeypatch):
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        task_wake,
        "report_finished",
        lambda task, *, stopped_on_request: seen.append((task["task_id"], stopped_on_request))
        or "wake",
    )
    chat = shell.start_shell_task("echo hi", cwd=str(tmp_path), session_id=SID, wake_on_finish=True)
    plain = shell.start_shell_task("echo hi", cwd=str(tmp_path), session_id=SID)
    for info in (chat, plain):
        _wait_finished(info["task_id"])
    time.sleep(0.2)
    assert registry.get_task(chat["task_id"])["meta"][task_wake.WAKE_META_KEY] is True
    assert task_wake.WAKE_META_KEY not in registry.get_task(plain["task_id"])["meta"]
    # Every finished task is offered; report_finished decides by the meta.
    assert {t for t, _ in seen} == {chat["task_id"], plain["task_id"]}


def test_a_stopped_command_is_reported_as_stopped_on_request(tmp_path, monkeypatch):
    seen: list[bool] = []
    monkeypatch.setattr(
        task_wake,
        "report_finished",
        lambda task, *, stopped_on_request: seen.append(stopped_on_request) or "skipped",
    )
    info = shell.start_shell_task(
        "sleep 30", cwd=str(tmp_path), session_id=SID, wake_on_finish=True
    )
    time.sleep(0.3)
    assert shell.stop_task(info["task_id"])
    _wait_finished(info["task_id"])
    time.sleep(0.2)
    assert seen == [True]


def test_ws_run_command_marks_a_chat_command_and_not_an_agents(tmp_path, monkeypatch):
    from server.workspace import executors

    monkeypatch.setattr(executors, "get_workspace_path", lambda: str(tmp_path))
    texts = {}
    for who, stamp in (("chat", {}), ("agent", {"__agent__": True})):
        text = executors._exec_ws_run_command(
            {"command": "ls", "run_in_background": True, "__session_id__": SID, **stamp}, "", []
        )
        texts[who] = text
    ids = {who: text.split("task_id=")[1].split()[0] for who, text in texts.items()}
    for task_id in ids.values():
        _wait_finished(task_id)
    assert registry.get_task(ids["chat"])["meta"].get(task_wake.WAKE_META_KEY) is True
    assert task_wake.WAKE_META_KEY not in registry.get_task(ids["agent"])["meta"]
    # The chat is told not to poll; the agent, with no wake, still polls.
    assert "comes back to you" in texts["chat"] and "do not poll" in texts["chat"]
    assert "Check on it with task_status" in texts["agent"]


def test_the_background_cap_fits_a_release_build():
    # The field build ran 1 h 43 min end to end.
    assert shell.MAX_RUNTIME_S >= 2 * 60 * 60


# ── what the model reads ────────────────────────────────────────────────────


def test_the_row_reads_as_data_in_a_user_turn():
    payload = task_wake.report_payload(_task(exit_code=2, status="failed", result_text="boom"))
    (msg,) = visible_chat_history([{"role": "task_report", "content": "", "taskReport": payload}])
    assert msg["role"] == "user"
    assert msg["content"].startswith(task_wake.HEAD)
    assert "Result: failed, exit code 2" in msg["content"]
    assert "make release" in msg["content"] and "boom" in msg["content"]
    assert "task_output {'task_id': 't1'}" in msg["content"]


def test_a_mid_turn_result_is_framed_as_data():
    payload = task_wake.report_payload(_task())
    text = _midturn_text(
        midturn_inbox.Entry(midturn_inbox.TASK_REPORT, task_wake.report_body(payload), payload)
    )
    assert text.startswith("<system-reminder>") and task_wake.MIDTURN_HEAD in text
    assert "not a message from the user" in text


def test_the_frames_reach_the_app():
    report = {"task_id": "t1"}
    assert _session_event_frame({"type": "task_report", "taskReport": report}) == {
        "task_report": report
    }
    assert _session_event_frame({"type": "task_wake", "taskWake": {"task_ids": ["t1"]}}) == {
        "task_wake": {"task_ids": ["t1"]}
    }
    show = {"terminal_id": "p1", "cwd": "/w", "opened": True}
    assert _session_event_frame({"type": "terminal_show", "terminalShow": show}) == {
        "terminal_show": show
    }


# ── a wake turn's evidence ──────────────────────────────────────────────────


def _wake_turn(payload: dict) -> list:
    return [
        {"role": "user", "content": "push it when the build passes"},
        {"role": "assistant", "content": "The build runs in the background."},
        {"role": "user", "content": task_wake.report_text(payload)},
        {"role": "user", "content": task_wake.WAKE_PROMPT},
    ]


def test_the_woken_command_is_a_call_of_the_turn():
    from server.goals.call_results import woken_calls

    pushed = task_wake.report_payload(
        _task(command="make release && git push origin main", result_text="main -> main")
    )
    calls, began = woken_calls(_wake_turn(pushed))
    (call,) = calls
    assert call.ok and call.name == "ws_run_command"
    assert "push" in {a.kind for a in call.acts}
    assert began is not None and began < time.time()

    failed = task_wake.report_payload(_task(status="failed", exit_code=1))
    (call,) = woken_calls(_wake_turn(failed))[0]
    assert not call.ok


def test_the_claim_guard_starts_the_turn_when_the_command_did():
    from types import SimpleNamespace

    from server.chat.claim_guard import ClaimGuard

    messages = _wake_turn(task_wake.report_payload(_task()))
    ctx = SimpleNamespace(
        turn_scope_id=None,
        session_id=SID,
        messages=messages,
        ws_path=None,
        plan_mode=False,
        cost_source="chat",
        policy=None,
        earlier_deliveries=[],
    )
    guard = ClaimGuard.for_turn(ctx)
    assert guard.started_at < time.time() - 3600


def test_text_without_a_report_head_carries_no_command():
    from server.goals.call_results import woken_calls

    body = task_wake.report_body(task_wake.report_payload(_task()))
    messages = [{"role": "user", "content": f"look at this:\n{body}"}]
    assert woken_calls(messages) == ([], None)
