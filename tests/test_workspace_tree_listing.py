"""The file tree lists what the disk holds: server/workspace/filesystem.py.

Dot entries and build folders used to be dropped, so a workspace showed no
dist, .venv or .env.example although Finder did. The listing now hides
nothing; search keeps its own skip rules so node_modules cannot flood it.
"""

import os

from server.workspace.filesystem import _ws_list_dir, _ws_search_files


def _seed(root):
    for rel in (
        ".venv/bin/python",
        ".git/HEAD",
        "dist/Capacity Planner.app/Contents/Info.plist",
        "node_modules/x/index.js",
        "__pycache__/a.pyc",
        "backend/app.py",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    for name in (".env.example", ".gitignore", "dist.zip", "README.md"):
        (root / name).write_text("x\n")


def test_every_entry_on_disk_is_listed(tmp_path):
    _seed(tmp_path)

    entries = _ws_list_dir(str(tmp_path))

    assert {e["name"] for e in entries} == set(os.listdir(tmp_path))
    kinds = {e["name"]: e["type"] for e in entries}
    assert kinds[".venv"] == kinds["dist"] == kinds["node_modules"] == "directory"
    assert kinds[".env.example"] == kinds["dist.zip"] == "file"


def test_a_dot_folder_lists_its_children(tmp_path):
    _seed(tmp_path)

    entries = _ws_list_dir(str(tmp_path), ".venv")

    assert [(e["path"], e["type"]) for e in entries] == [(".venv/bin", "directory")]


def test_search_still_skips_dot_entries_and_build_folders(tmp_path):
    _seed(tmp_path)

    found = {r["path"] for r in _ws_search_files(str(tmp_path), "")}

    assert "backend/app.py" in found
    assert not any(
        part.startswith(".") or part in ("dist", "node_modules", "__pycache__")
        for path in found
        for part in path.split("/")[:-1]
    )
