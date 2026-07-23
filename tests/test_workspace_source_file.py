"""/api/workspace/source-file backs the chat 'source' links opened in the dock.

Grounded-index answers cite files by *absolute* path, and those files live in
indexed folders that may not be the connected workspace (or there may be no
workspace connected at all). Unlike /file, this endpoint resolves such paths
against the indexed folders and returns readable content, while still refusing
to read anything outside a workspace/indexed root.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.workspace import router as ws_router


def _client():
    app = FastAPI()
    app.include_router(ws_router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Default: no connected workspace and no indexed folders — each test opts in.
    monkeypatch.setattr("server.workspace.routes.file_ops.get_workspace_path", lambda: None)
    import server.index.store as store

    monkeypatch.setattr(store, "list_indexed_workspaces", lambda: [])


def _index(monkeypatch, *roots):
    import server.index.store as store

    monkeypatch.setattr(store, "list_indexed_workspaces", lambda: [str(r) for r in roots])


def test_reads_indexed_file_outside_workspace(tmp_path, monkeypatch):
    # The reported bug: a citation into an indexed folder with NO workspace
    # connected. The absolute path must still resolve and return content.
    f = tmp_path / "notes.md"
    f.write_text("# Heading\n\nbody text")
    _index(monkeypatch, tmp_path)

    r = _client().get("/api/workspace/source-file", params={"path": str(f)})
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "markdown"
    assert data["content"] == "# Heading\n\nbody text"
    assert data["name"] == "notes.md"


def test_code_file_is_plain_text(tmp_path, monkeypatch):
    f = tmp_path / "main.py"
    f.write_text("print('hi')\n")
    _index(monkeypatch, tmp_path)

    data = _client().get("/api/workspace/source-file", params={"path": str(f)}).json()
    assert data["kind"] == "text"
    assert data["content"] == "print('hi')\n"


def test_rejects_path_outside_indexed_and_workspace_roots(tmp_path, monkeypatch):
    # A real file that lives in NO indexed/workspace root must not be readable —
    # the endpoint is not an arbitrary-file reader.
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    indexed = tmp_path / "indexed"
    indexed.mkdir()
    _index(monkeypatch, indexed)

    r = _client().get("/api/workspace/source-file", params={"path": str(outside)})
    assert r.status_code == 404


def test_rejects_traversal_escape(tmp_path, monkeypatch):
    # `..` that resolves outside the indexed root is blocked by realpath.
    indexed = tmp_path / "indexed"
    indexed.mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    _index(monkeypatch, indexed)

    escape = str(indexed / ".." / "secret.txt")
    r = _client().get("/api/workspace/source-file", params={"path": escape})
    assert r.status_code == 404


def test_unsupported_binary_gets_friendly_message(tmp_path, monkeypatch):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00\x00\x00\x18ftyp")
    _index(monkeypatch, tmp_path)

    data = _client().get("/api/workspace/source-file", params={"path": str(f)}).json()
    assert data["kind"] == "unsupported"
    assert "Finder" in data["message"]


def test_image_kind_and_raw_stream(tmp_path, monkeypatch):
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    f = tmp_path / "pic.png"
    f.write_bytes(png)
    _index(monkeypatch, tmp_path)
    c = _client()

    meta = c.get("/api/workspace/source-file", params={"path": str(f)}).json()
    assert meta["kind"] == "image"

    raw = c.get("/api/workspace/source-file", params={"path": str(f), "raw": "true"})
    assert raw.status_code == 200
    assert raw.headers["content-type"] == "image/png"
    assert raw.content == png


def test_head_raw_reports_size_without_body(tmp_path, monkeypatch):
    # The dock's spreadsheet size guard probes via HEAD before handing bytes to
    # the client-side parser. FastAPI does not auto-register HEAD on GET routes,
    # so the route declares it explicitly; FileResponse answers with the true
    # content-length and no body.
    f = tmp_path / "big.xlsx"
    f.write_bytes(b"x" * 12345)
    _index(monkeypatch, tmp_path)

    r = _client().head(f"/api/workspace/source-file?path={f}&raw=true")
    assert r.status_code == 200
    assert r.headers["content-length"] == "12345"
    assert r.content == b""


def test_oversized_text_returns_unsupported(tmp_path, monkeypatch):
    # Text is rendered unvirtualized client-side, so the JSON branch refuses to
    # inline more than _SOURCE_TEXT_MAX_BYTES and points at Finder instead.
    import server.workspace.routes.file_ops as routes

    f = tmp_path / "huge.log"
    f.write_text("x" * 1024)
    _index(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "_SOURCE_TEXT_MAX_BYTES", 100)

    data = _client().get("/api/workspace/source-file", params={"path": str(f)}).json()
    assert data["kind"] == "unsupported"
    assert "too large" in data["message"]


def test_relative_path_resolves_against_connected_workspace(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hello")
    monkeypatch.setattr("server.workspace.routes.file_ops.get_workspace_path", lambda: str(tmp_path))

    data = _client().get("/api/workspace/source-file", params={"path": "a.txt"}).json()
    assert data["kind"] == "text"
    assert data["content"] == "hello"
