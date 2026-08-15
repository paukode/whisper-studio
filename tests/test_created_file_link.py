"""A chat link to a file the assistant just wrote used to be whatever href the
model invented — often broken (WKWebView navigating to a nonexistent route) or
just inert. _do_write (server.approval.bootstrap, backs both the "write" and
"create" approval actions) now hands the model a ready-made #wsfile= link
with an explicit &open=os marker, so the client opens it with the OS default
app instead of leaving the model to guess at a URL.
"""

import asyncio
from unittest.mock import patch
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import workspace
from server.approval.bootstrap import _do_write
from server.workspace.routes import os_integration  # noqa: F401 — registers routes


@pytest.fixture(autouse=True)
def _pin_darwin(monkeypatch):
    """The argv assertions below pin the macOS branch (`open`). CI runs on
    Linux, where the route correctly picks xdg-open, so pin the platform
    rather than letting the expected argv follow the host."""
    monkeypatch.setattr(os_integration, "_system", lambda: "Darwin")


def test_do_write_output_includes_a_ready_made_open_os_link(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "get_workspace_path", lambda: str(tmp_path))

    outcome = asyncio.run(_do_write({"path": "report.md", "content": "hello"}))

    assert outcome.ok is True
    # Percent-encoding details are covered by test_created_file_link_encoding
    # (tests/test_index.py); here just confirm the link is present, points at
    # the absolute path, and carries the open-externally marker.
    assert "(#wsfile=" in outcome.output
    href = outcome.output.split("(#wsfile=", 1)[1].rstrip(")")
    path_part, _, params = href.partition("&")
    assert unquote(path_part) == str(tmp_path / "report.md")
    assert params == "open=os"


def test_do_write_link_has_no_line_range_unlike_a_citation(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "get_workspace_path", lambda: str(tmp_path))

    outcome = asyncio.run(_do_write({"path": "notes.txt", "content": "x"}))

    assert "&L=" not in outcome.output


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(workspace.router)
    return TestClient(app)


def test_open_with_accepts_absolute_path_in_the_connected_workspace(tmp_path, monkeypatch):
    # The created-file link is always absolute (server.index.citations); the
    # route used to only os.path.join a path onto the workspace root, which
    # happens to work for an absolute path too (os.path.join discards the
    # first argument), but never checked it against ANY indexed root the way
    # /reveal already does — this pins the /reveal-equivalent behavior.
    f = tmp_path / "report.md"
    f.write_text("hi")
    monkeypatch.setattr(os_integration, "get_workspace_path", lambda: str(tmp_path))

    with patch("server.workspace.routes.os_integration.subprocess.Popen") as popen:
        resp = _client().post("/api/workspace/open-with", json={"path": str(f)})

    assert resp.status_code == 200
    assert resp.json() == {"path": str(f), "opened": True}
    popen.assert_called_once_with(["open", str(f)])


def test_open_with_accepts_absolute_path_in_a_different_indexed_workspace(tmp_path, monkeypatch):
    # The user has since connected a different workspace, but the link points
    # at a file created in a PREVIOUSLY connected (still indexed) one — must
    # still open, mirroring /reveal's "any known indexed root" acceptance.
    other_ws = tmp_path / "other-workspace"
    other_ws.mkdir()
    f = other_ws / "report.md"
    f.write_text("hi")
    current_ws = tmp_path / "current-workspace"
    current_ws.mkdir()
    monkeypatch.setattr(os_integration, "get_workspace_path", lambda: str(current_ws))
    monkeypatch.setattr("server.index.store.list_indexed_workspaces", lambda: [str(other_ws)])

    with patch("server.workspace.routes.os_integration.subprocess.Popen") as popen:
        resp = _client().post("/api/workspace/open-with", json={"path": str(f)})

    assert resp.status_code == 200
    popen.assert_called_once_with(["open", str(f)])


def test_open_with_rejects_absolute_path_outside_any_indexed_workspace(tmp_path, monkeypatch):
    outside = tmp_path / "not-a-workspace"
    outside.mkdir()
    f = outside / "secret.md"
    f.write_text("hi")
    monkeypatch.setattr(os_integration, "get_workspace_path", lambda: str(tmp_path / "ws"))
    monkeypatch.setattr("server.index.store.list_indexed_workspaces", lambda: [])

    resp = _client().post("/api/workspace/open-with", json={"path": str(f)})

    assert resp.status_code == 404
