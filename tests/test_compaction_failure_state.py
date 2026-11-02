"""A failed compaction preserves saved history and closes its live inbox safely."""

import asyncio
import copy

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.chat import compaction
from server.chat.engine import midturn_inbox
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn
from server.infrastructure.sessions import router as sessions_router
from tests.test_compaction_failure_paths import _Adapter, _assert_failed_once, _history, _run


def test_compaction_failure_never_replaces_saved_session_history(monkeypatch):
    app = FastAPI()
    app.include_router(sessions_router)
    client = TestClient(app)
    session_id = "preserve-compaction-history"
    history = _history()
    response = client.put(
        f"/api/sessions/{session_id}",
        json={
            "id": session_id,
            "title": "Keep this conversation",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "chatHistory": history,
            "segments": [],
            "speakerNames": {},
        },
    )
    assert response.status_code == 200
    stored_before = client.get(f"/api/sessions/{session_id}").json()
    assert stored_before["chatHistory"] == history

    async def fail(*args, **kwargs):
        raise compaction.CompactionError()

    monkeypatch.setattr(compaction, "compact_messages_with_claude", fail)
    current = copy.deepcopy(history)
    frames = _run(_Adapter("overflow"), current, session_id=session_id)
    _assert_failed_once(frames)
    assert current == history
    stored_after = client.get(f"/api/sessions/{session_id}").json()
    assert stored_after["chatHistory"] == stored_before["chatHistory"]
    assert stored_after["title"] == stored_before["title"]


def test_message_received_during_failed_compaction_is_announced_before_done(monkeypatch):
    session_id = "compaction-late-message"
    notifications = []
    midturn_inbox.clear(session_id)
    monkeypatch.setattr(
        "server.notifications.record_notification", lambda **values: notifications.append(values)
    )

    async def fail(*args, **kwargs):
        assert midturn_inbox.push(session_id, "Do not lose this message")
        raise compaction.CompactionError()

    monkeypatch.setattr(compaction, "compact_messages_with_claude", fail)
    history = _history()
    original = copy.deepcopy(history)
    adapter = _Adapter("overflow")

    async def run():
        context = TurnContext(
            session_id=session_id,
            model_key="haiku",
            model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0",
            messages=history,
            adapter=adapter,
            policy=TurnPolicy(max_rounds=20, completion_gate=False),
            loop=asyncio.get_running_loop(),
            executor=None,
            cost_source="chat",
            memory_hooks=lambda messages: None,
            tool_catalog=lambda: ([], None),
            midturn_inbox=True,
        )
        return [frame async for frame in run_turn(context)]

    try:
        frames = asyncio.run(run())
        _assert_failed_once(frames)
        assert midturn_inbox.UNANSWERED in "".join(frames[:-1])
        assert not midturn_inbox.has_pending(session_id)
        assert not midturn_inbox.push(session_id, "This belongs to the next turn")
        assert history == original
        assert len(adapter.requests) == 1
        assert len(notifications) == 1
        assert notifications[0]["message"] == midturn_inbox.UNANSWERED
    finally:
        midturn_inbox.clear(session_id)


def test_failed_compaction_drains_prior_label_before_closing_inbox(monkeypatch):
    from server.chat.engine import activity_labels
    from server.infrastructure.errors import PromptTooLongError
    from tests.test_activity_labels import _Adapter as LabelAdapter
    from tests.test_activity_labels import _ctx, _labels, _payload, _tool_pipeline, read

    _tool_pipeline(monkeypatch)
    released = asyncio.Event()
    tasks = []

    async def caption():
        await released.wait()
        return _payload("prior-call")

    def start(*args):
        task = asyncio.create_task(caption())
        tasks.append(task)
        return task

    class Adapter(LabelAdapter):
        async def stream_round(self, messages, *args):
            if self.seen:
                raise PromptTooLongError("overflow")
            async for event in super().stream_round(messages, *args):
                yield event

    async def fail(*args, **kwargs):
        assert midturn_inbox.push("labels", "Keep the queued question")
        released.set()
        raise compaction.CompactionError()

    monkeypatch.setattr(activity_labels, "start_label_task", start)
    monkeypatch.setattr(compaction, "compact_messages_with_claude", fail)
    ctx = _ctx(Adapter([read("prior-call", "a.py")]))
    ctx.midturn_inbox = True
    midturn_inbox.clear(ctx.session_id)

    async def run():
        return [frame async for frame in run_turn(ctx)]

    try:
        frames = asyncio.run(run())
        _assert_failed_once(frames)
        assert _labels(frames) == [_payload("prior-call")]
        label_at = next(i for i, frame in enumerate(frames) if '"activity_label"' in frame)
        unread_at = next(i for i, frame in enumerate(frames) if midturn_inbox.UNANSWERED in frame)
        assert label_at < unread_at < len(frames) - 1
        assert tasks and all(task.done() for task in tasks)
        assert not midturn_inbox.push(ctx.session_id, "next turn")
        assert ctx.messages[-1]["content"][0]["content"] == "output of prior-call"
    finally:
        midturn_inbox.clear(ctx.session_id)
