"""A session's updated_at is the last time it changed, not the last time the
client saved it.

The client re-saves every open session on a 30s timer (and on switch,
eviction and unload) with its own clock as updatedAt, so opening an old
session and doing nothing used to move it into Today. A save that changes
nothing keeps the stored time; a real edit still moves it; the unload beacon
follows the same rule as PUT.
"""

import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure import sessions
from server.infrastructure.sessions import router as sessions_router

OPENED = "2026-09-11T19:57:18.168Z"
LATER = "2026-09-25T15:22:28.949Z"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(sessions_router)
    return TestClient(app)


def _session(**overrides) -> dict:
    """A session as the client holds it: a turn with a tool card, a chart and
    a question, plus a short transcript."""
    session = {
        "id": "s1",
        "title": "Repository Branches List",
        "customTitle": False,
        "generatedTitle": True,
        "createdAt": "2026-09-11T19:40:00.000Z",
        "updatedAt": OPENED,
        "chatHistory": [
            {"role": "user", "content": "list the branches", "timestamp": "2026-09-11T19:41Z"},
            {
                "role": "assistant",
                "content": "Here they are. Delete the stale ones?",
                "timestamp": "2026-09-11T19:42Z",
                "toolUse": [{"name": "git_branches", "input": {"remote": True}, "output": "main"}],
                "visuals": [{"kind": "chart", "spec": {"mark": "bar"}}],
                "userQuestion": {
                    "question": "Delete them?",
                    "options": ["Yes", "No"],
                    "toolUseId": "tu_1",
                },
            },
        ],
        "segments": [
            {
                "id": "g1",
                "speaker": "SPEAKER_00",
                "text": "hello",
                "timestamp": 1.5,
                "edited": False,
            }
        ],
        "speakerNames": {"SPEAKER_00": "Marta"},
    }
    session.update(copy.deepcopy(overrides))
    return session


def _save(client, route: str, body: dict) -> None:
    if route == "put":
        r = client.put(f"/api/sessions/{body['id']}", json=body)
    else:
        r = client.post(f"/api/sessions/{body['id']}/beacon", json=body)
    assert r.status_code == 200, r.text


def _updated_at(client, sid: str = "s1") -> str:
    (row,) = [r for r in client.get("/api/sessions").json() if r["id"] == sid]
    return row["date"]


def _add_message(s):
    s["chatHistory"].append({"role": "user", "content": "and the tags?", "timestamp": "t3"})


def _resize_picture(s):
    s["chatHistory"][1]["mediaSizes"] = {"viz-0": {"w": 480}}


def _answer_question(s):
    s["chatHistory"][1]["userQuestion"]["answered"] = True


def _add_segment(s):
    s["segments"].append(
        {"id": "g2", "speaker": "SPEAKER_01", "text": "hi", "timestamp": 3.0, "edited": False}
    )


def _edit_segment(s):
    s["segments"][0].update(text="hello everyone", edited=True)


def _name_speaker(s):
    s["speakerNames"]["SPEAKER_00"] = "Marta K."


def _generated_title(s):
    s["title"] = "Branch Cleanup"


EDITS = [
    _add_message,
    _resize_picture,
    _answer_question,
    _add_segment,
    _edit_segment,
    _name_speaker,
    _generated_title,
]


@pytest.mark.parametrize("route", ["put", "beacon"])
def test_a_save_that_changes_nothing_keeps_updated_at(client, route):
    _save(client, "put", _session())
    # What the client loaded on open, sent back by the periodic loop (or the
    # unload beacon) with a later clock.
    loaded = client.get("/api/sessions/s1").json()
    idle = _session(
        updatedAt=LATER,
        chatHistory=loaded["chatHistory"],
        segments=loaded["segments"],
        speakerNames=loaded["speakerNames"],
    )
    _save(client, route, idle)
    _save(client, route, idle)
    assert _updated_at(client) == OPENED


@pytest.mark.parametrize("route", ["put", "beacon"])
@pytest.mark.parametrize("edit", EDITS, ids=lambda f: f.__name__.lstrip("_"))
def test_an_edit_moves_updated_at(client, route, edit):
    _save(client, "put", _session())
    edited = _session(updatedAt=LATER)
    edit(edited)
    _save(client, route, edited)
    assert _updated_at(client) == LATER


def test_values_the_browser_reserializes_are_not_an_edit(client):
    """Rows the server appends are written by Python, so a float is stored as
    2.0; the browser parses it and writes it back as 2. Same data."""
    _save(client, "put", _session())
    row = {"role": "task_event", "content": "", "timestamp": "t9", "taskEvent": {"secs": 2.0}}
    sessions._append_message_sync("s1", row)
    appended = _updated_at(client)
    history = client.get("/api/sessions/s1").json()["chatHistory"]
    history[-1]["taskEvent"]["secs"] = 2
    _save(client, "put", _session(updatedAt=LATER, chatHistory=history))
    assert _updated_at(client) == appended


def test_a_save_without_a_row_the_server_appended_is_not_an_edit(client):
    """A cron firing appends to chat_history and moves updated_at. A client
    that missed the event re-saves its older copy; the merge keeps the row,
    so the save changes nothing and must not move the time back either."""
    _save(client, "put", _session())
    sessions._append_message_sync(
        "s1", {"role": "cron_event", "content": "", "timestamp": "t8", "cronEvent": {}}
    )
    appended = _updated_at(client)
    assert appended != OPENED
    _save(client, "put", _session(updatedAt=LATER))
    assert _updated_at(client) == appended
    roles = [m["role"] for m in client.get("/api/sessions/s1").json()["chatHistory"]]
    assert roles.count("cron_event") == 1


def test_a_rename_moves_updated_at_and_the_save_after_it_keeps_it(client):
    """The sidebar renames with a PATCH, then the full save follows two
    seconds later. The PATCH is the edit; the save finds the title stored."""
    _save(client, "put", _session())
    rename = {"title": "Branch Cleanup", "customTitle": True}
    assert client.patch("/api/sessions/s1/title", json=rename).status_code == 200
    renamed = _updated_at(client)
    assert renamed != OPENED

    after = _session(
        updatedAt=LATER, title="Branch Cleanup", customTitle=True, generatedTitle=False
    )
    _save(client, "put", after)
    assert _updated_at(client) == renamed

    client.patch("/api/sessions/s1/title", json=rename)
    assert _updated_at(client) == renamed
