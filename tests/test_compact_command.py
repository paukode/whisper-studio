"""The /compact command (server/chat/compact_route.py): one summary of the
whole prompt history, kept as a backend-owned compact_summary row that
stands in for every row before it."""

import asyncio
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.openai_bedrock.runtime  # noqa: F401
from server.chat import compaction
from server.chat.compact_route import router
from server.infrastructure import config, oneshot, sessions


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch):
    cfg = {"model_mode": "cloud", "chat_models": {"claude": "global.anthropic.claude-haiku-4-5"}}
    monkeypatch.setattr(config, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(compaction, "thresholds_for", lambda *a, **k: (100_000, 120_000))
    monkeypatch.setattr("server.chat.compaction_log._append", lambda *a, **k: None)
    return cfg


def history(n=10):
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i} " + "x" * 2000}
        for i in range(n)
    ]


def test_the_summary_is_asked_to_keep_the_users_note(monkeypatch):
    summary = Mock(return_value="Short summary.")
    monkeypatch.setattr(oneshot, "one_shot", summary)
    text, before, after = asyncio.run(
        compaction.compact_on_request(history(), "claude", "s1", note="the MCP setup steps")
    )
    prompt = summary.call_args.args[1]
    assert "the MCP setup steps" in prompt
    assert text.startswith(compaction.SUMMARY_HEADER) and "Short summary." in text
    assert after < before


def test_a_summary_that_does_not_shrink_replaces_nothing(monkeypatch):
    monkeypatch.setattr(oneshot, "one_shot", lambda *a, **k: "y" * 50_000)
    with pytest.raises(compaction.CompactionError):
        asyncio.run(compaction.compact_on_request(history(2), "claude", "s1"))


def test_the_route_appends_a_backend_owned_row(monkeypatch):
    monkeypatch.setattr(oneshot, "one_shot", lambda *a, **k: "Short summary.")
    appended = []

    async def append(session_id, row):
        appended.append((session_id, row))
        return True

    monkeypatch.setattr(sessions, "append_message", append)
    app = FastAPI()
    app.include_router(router)
    resp = TestClient(app).post(
        "/api/sessions/s1/compact",
        json={"history": history(), "note": "keep the plan", "model": "claude"},
    )
    assert resp.status_code == 200
    row = resp.json()
    assert row["role"] == "compact_summary"
    assert row["compactSummary"]["note"] == "keep the plan"
    assert row["compactSummary"]["messages"] == 10
    assert appended == [("s1", row)]
    assert row["role"] not in sessions.PROMPT_ROLES  # a frontend save keeps it


def test_a_failed_summary_appends_nothing(monkeypatch):
    monkeypatch.setattr(oneshot, "one_shot", Mock(side_effect=RuntimeError("down")))
    append = Mock()
    monkeypatch.setattr(sessions, "append_message", append)
    app = FastAPI()
    app.include_router(router)
    resp = TestClient(app).post(
        "/api/sessions/s1/compact", json={"history": history(), "model": "claude"}
    )
    assert resp.status_code == 502
    append.assert_not_called()


def test_the_model_reads_the_summary_and_only_what_follows_it():
    rows = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "compact_summary", "content": "[Context summary] first"},
        {"role": "user", "content": "middle"},
        {"role": "compact_summary", "content": "[Context summary] second"},
        {"role": "user", "content": "new question"},
        {"role": "cron_event", "content": "ui only"},
    ]
    assert sessions.visible_chat_history(rows) == [
        {"role": "user", "content": "[Context summary] second"},
        {"role": "user", "content": "new question"},
    ]
