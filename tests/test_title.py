"""Tests for the deterministic cleanup applied to generated session titles."""

import pytest

from server.chat.routes import _clean_title


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('"Cron Job Fix"', "Cron Job Fix"),  # strip surrounding quotes
        ("'Weekly Report'", "Weekly Report"),  # strip single quotes
        ("Title: Weekly Report", "Weekly Report"),  # drop a leading "Title:"
        ("Foo — Bar – Baz", "Foo Bar Baz"),  # em/en dashes removed
        ("one two three four five six seven", "one two three four five six"),  # clamp to 6 words
        ("Trailing punctuation here.", "Trailing punctuation here"),  # trailing punct
        ("  spaced   out   title  ", "spaced out title"),  # collapse whitespace
        ("", "New Conversation"),  # empty falls back
        ("   ", "New Conversation"),  # whitespace falls back
        # Pure-words rules (Q3-style labels kept; dates / symbols dropped):
        ("Q3 Marketing Budget Plan", "Q3 Marketing Budget Plan"),  # keep alphanumeric label
        ("S3 Bucket Cleanup", "S3 Bucket Cleanup"),  # keep alphanumeric label
        ("Meeting Notes July 2026", "Meeting Notes"),  # drop month + year
        ("Release 2026-07-05 Summary", "Release Summary"),  # drop ISO date
        ("Standup 7/6 Recap", "Standup Recap"),  # drop slash date
        ("Budget & Timeline Review", "Budget and Timeline Review"),  # & becomes "and"
        ("Sprint Retro on Monday", "Sprint Retro on"),  # drop weekday name
        ("Fix #42 in Parser", "Fix 42 in Parser"),  # hashtag symbol dropped, 42 kept
        ("Launch on 6th of the Month", "Launch on of the Month"),  # drop ordinal date
    ],
)
def test_clean_title(raw, expected):
    assert _clean_title(raw) == expected


def test_clean_title_at_most_six_words():
    out = _clean_title("alpha beta gamma delta epsilon zeta eta theta")
    assert len(out.split()) == 6


def test_clean_title_length_cap():
    out = _clean_title("word " * 40)
    assert len(out) <= 60


def test_clean_title_all_dates_falls_back():
    assert _clean_title("2026 July Monday") == "New Conversation"


# ── Local mode: titles never leave this Mac ───────────────────────────────────


def _local_mode_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.chat import routes

    # Patched where the endpoint resolves it (a lazy import inside the handler).
    monkeypatch.setattr("server.infrastructure.model_mode.current_mode", lambda *a, **k: "local")

    requested: list[bool] = []

    def _no_bedrock():
        requested.append(True)
        raise AssertionError("Bedrock client requested in Local mode")

    monkeypatch.setattr(routes, "_get_bedrock_client", _no_bedrock)
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), requested


def test_local_mode_title_comes_from_the_first_user_line(monkeypatch):
    client, requested = _local_mode_client(monkeypatch)
    text = "User: Plan the Krakow offsite budget\nAssistant: Sure, here is a plan."
    title = client.post("/api/generate-title", json={"text": text}).json()["title"]
    assert title == _clean_title("Plan the Krakow offsite budget")
    assert requested == []


def test_local_mode_title_of_unlabelled_text_uses_its_first_line(monkeypatch):
    client, requested = _local_mode_client(monkeypatch)
    title = client.post("/api/generate-title", json={"text": "\n  Fix the parser\nmore"}).json()
    assert title["title"] == _clean_title("Fix the parser")
    assert requested == []


# ── Cloud: a failed title is an error, never a made-up name ──────────────────


def _cloud_client(monkeypatch, invoke):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.chat import routes

    monkeypatch.setattr("server.infrastructure.model_mode.current_mode", lambda *a, **k: "cloud")
    monkeypatch.setattr(routes, "_get_chat_models", lambda: {"haiku": "anthropic.haiku-x"})
    monkeypatch.setattr(routes, "_get_bedrock_client", lambda: object())
    monkeypatch.setattr("server.costs.calls.invoke_claude", invoke)
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_a_failed_title_call_answers_with_an_error_status(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("AccessDeniedException: no access")

    client = _cloud_client(monkeypatch, _boom)
    r = client.post("/api/generate-title", json={"text": "User: Plan the offsite"})
    assert r.status_code == 502
    assert "no access" in r.json()["error"]
    assert "title" not in r.json()


def test_an_empty_model_reply_is_an_error_not_a_default_title(monkeypatch):
    client = _cloud_client(monkeypatch, lambda *a, **k: {"content": []})
    r = client.post("/api/generate-title", json={"text": "User: Plan the offsite"})
    assert r.status_code == 502


def test_a_generated_title_is_cleaned(monkeypatch):
    reply = {"content": [{"type": "text", "text": '"Offsite Budget Plan"'}]}
    client = _cloud_client(monkeypatch, lambda *a, **k: reply)
    r = client.post("/api/generate-title", json={"text": "User: Plan the offsite"})
    assert r.json() == {"title": "Offsite Budget Plan"}
