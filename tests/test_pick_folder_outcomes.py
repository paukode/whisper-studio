"""The native folder picker reports Cancel as cancelled and any other
osascript failure as an error with its message, never as a quiet cancel."""

import asyncio

import pytest
from fastapi.testclient import TestClient


def _fake_dialog(monkeypatch, returncode: int, out: bytes, err: bytes) -> None:
    class _Dialog:
        def __init__(self):
            self.returncode = returncode

        async def communicate(self):
            return out, err

        def kill(self):
            pass

        async def wait(self):
            return returncode

    async def fake_exec(*args, **kwargs):
        assert args[0] == "osascript"
        return _Dialog()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


@pytest.fixture
def client():
    from fastapi import FastAPI

    from server.workspace import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_a_picked_folder_is_returned(client, monkeypatch):
    _fake_dialog(monkeypatch, 0, b"/Users/someone/picked/\n", b"")

    assert client.get("/api/workspace/pick-folder").json() == {"path": "/Users/someone/picked"}


def test_cancel_is_reported_as_cancelled(client, monkeypatch):
    _fake_dialog(monkeypatch, 1, b"", b"execution error: User canceled. (-128)\n")

    assert client.get("/api/workspace/pick-folder").json() == {"path": None, "cancelled": True}


def test_any_other_failure_is_an_error_with_its_message(client, monkeypatch):
    _fake_dialog(
        monkeypatch, 1, b"", b"execution error: Not authorized to send Apple events. (-1743)\n"
    )

    resp = client.get("/api/workspace/pick-folder")

    assert resp.status_code == 500
    assert resp.json() == {
        "error": "The folder dialog failed: execution error: Not authorized to send Apple events. (-1743)"
    }
