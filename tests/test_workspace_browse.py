"""ws_browse lists subdirectories and the folder's files (files capped, with a
full count) so the connect dialog can preview what's inside a folder."""

import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.workspace import router as ws_router


def _client():
    app = FastAPI()
    app.include_router(ws_router)
    return TestClient(app)


def test_browse_lists_files_and_dirs(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.md").write_text("y")
    (tmp_path / ".hidden").write_text("z")  # dotfiles skipped in both lists

    data = _client().get("/api/workspace/browse", params={"path": str(tmp_path)}).json()

    # Directories: unchanged behaviour (dirs + rich entries).
    assert data["dirs"] == ["sub"]
    assert [e["name"] for e in data["entries"]] == ["sub"]
    # Files: new — names with mtimes, dotfiles excluded.
    file_names = sorted(f["name"] for f in data["files"])
    assert file_names == ["a.txt", "b.md"]
    assert data["file_total"] == 2
    assert all("mtime" in f for f in data["files"])


def test_browse_caps_files_to_newest_but_reports_total(tmp_path):
    # 260 files with strictly increasing mtimes — the cap must keep the 250
    # NEWEST (so the UI's "newest first" sort stays honest), not the
    # alphabetically-first 250.
    for i in range(260):
        f = tmp_path / f"f{i:03d}.txt"
        f.write_text("x")
        os.utime(f, (1000 + i, 1000 + i))

    data = _client().get("/api/workspace/browse", params={"path": str(tmp_path)}).json()

    assert data["file_total"] == 260  # full count reported
    assert len(data["files"]) == 250  # FILE_CAP — never silently unbounded
    names = {f["name"] for f in data["files"]}
    # The 10 oldest are dropped; the 250 newest are kept.
    assert "f000.txt" not in names and "f009.txt" not in names
    assert "f010.txt" in names and "f259.txt" in names
