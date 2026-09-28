"""A session's workspace folder is the server's to record.

The sidebar's "Open workspace in" opens sessions.workspace_path. The server
writes it as a chat turn's stream closes, with the folder connected then, and
nothing else writes it. Client saves (the PUT and the unload beacon) never
do, whatever the body carries, so the recorded folder survives every save and
an identical save still keeps updated_at.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure.sessions import _upsert_session, record_session_workspace
from server.infrastructure.sessions import router as sessions_router
from server.workspace.state import connect_workspace, save_workspace_config
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, run_chat_turn, text_block

OPENED = "2026-09-11T19:57:18.168Z"
LATER = "2026-09-25T15:22:28.949Z"
FOLDER = "/Users/me/code/parser"
ROUTES = ["put", "beacon"]


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(sessions_router)
    return TestClient(app)


def _client_save(**overrides) -> dict:
    """A save as the browser sends it: its Session type has no workspacePath."""
    body = {
        "id": "s1",
        "title": "Split the parser",
        "customTitle": False,
        "generatedTitle": True,
        "createdAt": "2026-09-11T19:40:00.000Z",
        "updatedAt": OPENED,
        "chatHistory": [{"role": "user", "content": "split the parser", "timestamp": "t1"}],
        "segments": [],
        "speakerNames": {},
    }
    body.update(overrides)
    return body


def _save(client, route: str, body: dict) -> None:
    if route == "put":
        r = client.put(f"/api/sessions/{body['id']}", json=body)
    else:
        r = client.post(f"/api/sessions/{body['id']}/beacon", json=body)
    assert r.status_code == 200, r.text


def _workspace(client, sid: str = "s1") -> str:
    return client.get(f"/api/sessions/{sid}").json()["workspacePath"]


def _summary(client, sid: str = "s1") -> dict:
    """The list row, which is what the sidebar reads."""
    (row,) = [r for r in client.get("/api/sessions").json() if r["id"] == sid]
    return row


def _add_message(body: dict) -> dict:
    body["chatHistory"].append({"role": "assistant", "content": "done", "timestamp": "t2"})
    return body


class _ModelThatActs(FakeBedrockClient):
    """Answers in one round, running ``during`` while the turn is in flight."""

    def __init__(self, during=None):
        super().__init__([[msg_start(), *text_block("Done."), *msg_end()]])
        self._during = during

    def invoke_model_with_response_stream(self, *args, **kwargs):
        if self._during:
            self._during()
        return super().invoke_model_with_response_stream(*args, **kwargs)


def _turn(monkeypatch, during=None, sid: str = "s1") -> None:
    lines = run_chat_turn(
        monkeypatch, _ModelThatActs(during), {"session_id": sid, "question": "split it"}
    )
    assert lines[-1] == "[DONE]"


def test_a_chat_turn_records_the_connected_folder(client, monkeypatch, tmp_path):
    _save(client, "put", _client_save())
    folder = connect_workspace(str(tmp_path))

    _turn(monkeypatch)

    assert _workspace(client) == folder
    assert _summary(client)["workspacePath"] == folder
    # Recording is not an edit: the session keeps its place in the list.
    assert _summary(client)["date"] == OPENED


def test_a_turn_with_nothing_connected_keeps_the_last_folder(client, monkeypatch, tmp_path):
    _save(client, "put", _client_save())
    folder = connect_workspace(str(tmp_path))
    _turn(monkeypatch)
    save_workspace_config({"path": None})

    _turn(monkeypatch)

    assert _workspace(client) == folder


def test_a_folder_the_turn_connects_itself_is_the_one_recorded(client, monkeypatch, tmp_path):
    """Like git_clone connecting the repo it cloned, mid-turn."""
    (tmp_path / "before").mkdir()
    (tmp_path / "cloned").mkdir()
    _save(client, "put", _client_save())
    connect_workspace(str(tmp_path / "before"))
    connected = {}

    def _the_turn_connects_its_clone():
        connected["path"] = connect_workspace(str(tmp_path / "cloned"))

    _turn(monkeypatch, during=_the_turn_connects_its_clone)

    assert _workspace(client) == connected["path"]


def test_a_turn_whose_folder_the_user_let_go_of_keeps_the_last_folder(
    client, monkeypatch, tmp_path
):
    """Connecting another folder from the UI mid-turn (perhaps for another
    session) releases the running turn: its remaining workspace tools are
    refused, so the folder connected in its place is not one it ran in."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _save(client, "put", _client_save())
    record_session_workspace("s1", FOLDER)
    connect_workspace(str(tmp_path / "a"))

    def _the_user_connects_another_folder():
        connect_workspace(str(tmp_path / "b"), by_user=True)

    _turn(monkeypatch, during=_the_user_connects_another_folder)

    assert _workspace(client) == FOLDER


def test_a_turn_that_fails_to_start_records_nothing(client, monkeypatch, tmp_path):
    from server.chat import routes

    _save(client, "put", _client_save())
    connect_workspace(str(tmp_path))

    def _unreadable_config(*args, **kwargs):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(routes, "latch_session", _unreadable_config)

    _turn(monkeypatch)

    assert _workspace(client) == ""


def test_a_session_created_while_its_first_turn_runs_records_the_folder(
    client, monkeypatch, tmp_path
):
    """The empty composer creates the session and sends its first turn at
    once, so the row can land after the turn has started."""
    folder = connect_workspace(str(tmp_path))
    body = _client_save()

    def _client_creates_the_row():
        _upsert_session(
            "s1",
            title=body["title"],
            custom_title=0,
            generated_title=1,
            created_at=body["createdAt"],
            updated_at=body["updatedAt"],
            segments="[]",
            chat_history_frontend=body["chatHistory"],
            speaker_names="{}",
        )

    _turn(monkeypatch, during=_client_creates_the_row)

    assert _workspace(client) == folder


CARRIED = {
    "without the field": {},
    "with another folder": {"workspacePath": "/Users/me/code/elsewhere"},
    "with an empty folder": {"workspacePath": ""},
    "with null": {"workspacePath": None},
}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("carried", CARRIED.values(), ids=CARRIED.keys())
def test_a_client_save_never_writes_the_folder(client, route, carried):
    _save(client, "put", _client_save())
    record_session_workspace("s1", FOLDER)

    _save(client, route, _add_message(_client_save(updatedAt=LATER, **carried)))

    assert _workspace(client) == FOLDER
    assert _summary(client)["workspacePath"] == FOLDER
    # The save itself landed.
    assert len(client.get("/api/sessions/s1").json()["chatHistory"]) == 2


@pytest.mark.parametrize("route", ROUTES)
def test_an_identical_save_keeps_updated_at_beside_a_recorded_folder(client, route):
    """The save does not write the folder, so it does not compare it either:
    a body naming another folder is still a save that changes nothing."""
    _save(client, "put", _client_save())
    record_session_workspace("s1", FOLDER)

    _save(client, route, _client_save(updatedAt=LATER, workspacePath="/Users/me/code/elsewhere"))

    assert _summary(client)["date"] == OPENED
    assert _workspace(client) == FOLDER


def test_recording_changes_only_the_folder(client):
    _save(client, "put", _client_save())
    before = client.get("/api/sessions/s1").json()

    record_session_workspace("s1", FOLDER)

    assert client.get("/api/sessions/s1").json() == {**before, "workspacePath": FOLDER}


def test_recording_never_adds_a_session(client):
    record_session_workspace("never-saved", FOLDER)

    assert all(r["id"] != "never-saved" for r in client.get("/api/sessions").json())


@pytest.mark.parametrize("route", ROUTES)
def test_a_new_session_starts_without_a_folder(client, route):
    _save(client, route, _client_save())

    assert _workspace(client) == ""
