"""The editor's save (POST /api/workspace/write) never writes text over an
Office document: a .docx, .xlsx or .pptx is a zip package, and a text save
destroyed it (the Word viewer's old Edit button opened an empty editor, and
Cmd+S saved that over the document)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.workspace import router as ws_router
from server.workspace.routes import file_ops


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(file_ops, "get_workspace_path", lambda: str(root))
    return root


def _client():
    app = FastAPI()
    app.include_router(ws_router)
    return TestClient(app)


@pytest.mark.parametrize("name", ["report.docx", "data.xlsx", "deck.pptx", "OLD.DOCX", "sheet.xls"])
def test_a_text_save_over_an_office_document_is_refused(ws, name):
    package = b"PK\x03\x04 office package bytes"
    (ws / name).write_bytes(package)
    resp = _client().post("/api/workspace/write", json={"path": name, "content": ""})
    assert resp.status_code == 415
    assert "nothing was written" in resp.json()["error"]
    assert (ws / name).read_bytes() == package


def test_a_new_office_file_is_not_created_as_text(ws):
    resp = _client().post("/api/workspace/write", json={"path": "new.docx", "content": ""})
    assert resp.status_code == 415
    assert not (ws / "new.docx").exists()


def test_a_text_file_still_saves(ws):
    resp = _client().post("/api/workspace/write", json={"path": "notes.md", "content": "hi\n"})
    assert resp.status_code == 200
    assert (ws / "notes.md").read_text() == "hi\n"
