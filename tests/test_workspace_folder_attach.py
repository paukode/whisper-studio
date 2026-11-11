"""A folder picked from the composer's @ menu: the search offers folders too,
and attaching one stores its listing as the message's document."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import attachments
from server.workspace.filesystem import _ws_search_files


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "src" / "components").mkdir(parents=True)
    (tmp_path / "src" / "components" / "Button.tsx").write_text("x")
    (tmp_path / "src" / "app.ts").write_text("x")
    (tmp_path / "node_modules" / "src").mkdir(parents=True)
    return tmp_path


def test_the_search_offers_a_folder_ahead_of_its_files_only_when_asked(ws):
    assert _ws_search_files(str(ws), "src") == [
        {"path": "src/app.ts", "binary": False},
        {"path": "src/components/Button.tsx", "binary": False},
    ]
    assert _ws_search_files(str(ws), "comp", folders=True) == [
        {"path": "src/components", "folder": True},
        {"path": "src/components/Button.tsx", "binary": False},
    ]


@pytest.fixture
def client(ws, monkeypatch):
    from server.workspace import router
    from server.workspace.routes import browse

    stored: dict[str, dict] = {}

    async def _store(aid, record):
        stored[aid] = record

    monkeypatch.setattr(attachments, "_store", _store)
    monkeypatch.setattr(browse, "get_workspace_path", lambda: str(ws))
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    c.stored = stored
    return c


def test_an_attached_folder_carries_its_listing(client):
    r = client.post("/api/workspace/attach-folder", json={"path": "src"})

    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "src/"
    assert client.stored[body["id"]]["text"] == (
        "[Folder src/ in the workspace: 2 entries]\napp.ts\ncomponents/"
    )


@pytest.mark.parametrize("path", ["", "../", "src/app.ts", "missing"])
def test_a_path_that_is_no_workspace_folder_is_refused(client, path):
    r = client.post("/api/workspace/attach-folder", json={"path": path})

    assert r.status_code == 404
    assert client.stored == {}
