"""server.chat.engine.midturn_inbox: the per-session queue that lets a message
sent while a turn is already running reach that SAME turn instead of being
refused (old behavior: HTTP 409 SESSION_BUSY) or silently dropped by the
composer. See server/chat/routes.py's SESSION_BUSY short-circuit and
runner.py's per-round drain."""

from server.chat.engine import midturn_inbox


def test_push_then_drain_returns_in_order():
    midturn_inbox.drain("s1")  # clear any cross-test leftovers
    midturn_inbox.push("s1", "first")
    midturn_inbox.push("s1", "second")
    assert midturn_inbox.has_pending("s1") is True
    assert midturn_inbox.drain("s1") == ["first", "second"]
    # Drain empties it.
    assert midturn_inbox.drain("s1") == []
    assert midturn_inbox.has_pending("s1") is False


def test_sessions_are_isolated():
    midturn_inbox.drain("a")
    midturn_inbox.drain("b")
    midturn_inbox.push("a", "for a")
    assert midturn_inbox.has_pending("b") is False
    assert midturn_inbox.drain("b") == []
    assert midturn_inbox.drain("a") == ["for a"]


def test_blank_or_missing_text_is_not_queued():
    midturn_inbox.drain("s2")
    midturn_inbox.push("s2", "")
    midturn_inbox.push("s2", "   ")
    midturn_inbox.push("s2", None)
    assert midturn_inbox.has_pending("s2") is False


def test_round_loop_folds_pending_message_into_last_user_message(monkeypatch):
    # The exact mechanism runner.py uses: a pending message must land as an
    # appended text block on the LAST user-role message, the same place
    # loop_hints.inject_reminder puts wind-down reminders — never lost, never
    # forking the message list into a separate turn.
    from server.chat.loop_hints import inject_reminder

    midturn_inbox.drain("s3")
    midturn_inbox.push("s3", "actually, stop and give me what you have")

    messages = [{"role": "user", "content": "original question"}]
    for pending in midturn_inbox.drain("s3"):
        wrapped = f"<user_message_mid_turn>{pending}</user_message_mid_turn>"
        assert inject_reminder(messages, wrapped) is True

    content = messages[-1]["content"]
    assert isinstance(content, list)
    assert any("stop and give me what you have" in b.get("text", "") for b in content)
