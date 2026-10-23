"""The file tree follows the disk: server/workspace/watcher.py.

The watcher reduces raw filesystem events to one ``workspace_changed`` payload
naming the folders whose listing may differ and the files whose content
changed, under the listing's own ignore rules, and publishes it app-wide.
"""

import asyncio
import threading
import time

from watchfiles import Change

from server.workspace.watcher import WorkspaceWatcher, coalesce


def test_an_added_or_deleted_entry_names_its_parent_folder(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "new.py").write_text("x = 1\n")

    ev = coalesce(
        str(tmp_path),
        [
            (Change.added, str(tmp_path / "pkg" / "new.py")),
            (Change.deleted, str(tmp_path / "old.md")),
        ],
    )

    assert ev["type"] == "workspace_changed"
    assert ev["root"] == str(tmp_path)
    assert ev["dirs"] == ["", "pkg"]
    # A file that appeared may be an atomic save over an open tab's file.
    assert ev["files"] == ["pkg/new.py"]


def test_a_changed_file_is_named_without_touching_any_listing(tmp_path):
    (tmp_path / "notes.md").write_text("one\n")

    ev = coalesce(str(tmp_path), [(Change.modified, str(tmp_path / "notes.md"))])

    assert ev["dirs"] == []
    assert ev["files"] == ["notes.md"]


def test_a_new_folder_names_its_parent_and_a_touched_folder_names_itself(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "assets").mkdir()

    ev = coalesce(
        str(tmp_path),
        [
            (Change.added, str(tmp_path / "docs" / "assets")),
            (Change.modified, str(tmp_path / "docs")),
        ],
    )

    # The folder itself existed, so the root's listing is not in question.
    assert ev["dirs"] == ["docs"]
    assert ev["files"] == []


def test_dot_entries_and_build_folders_are_reported_like_any_other(tmp_path):
    # The listing hides nothing, so an open .venv or dist must follow the disk too.
    paths = [
        tmp_path / ".venv",
        tmp_path / ".git" / "HEAD",
        tmp_path / "dist" / "app.js",
        tmp_path / "frontend" / "node_modules" / "x" / "index.js",
        tmp_path / ".env.example",
    ]
    for p in paths[1:]:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    paths[0].mkdir()

    ev = coalesce(str(tmp_path), [(Change.added, str(p)) for p in paths])

    assert ev["dirs"] == ["", ".git", "dist", "frontend/node_modules/x"]
    assert ev["files"] == sorted(str(p.relative_to(tmp_path)) for p in paths[1:])


def test_too_many_folders_are_not_named_so_the_ui_reloads_all_it_has(tmp_path):
    changes = [(Change.deleted, str(tmp_path / f"d{i}" / "f.txt")) for i in range(201)]

    ev = coalesce(str(tmp_path), changes)

    assert ev["dirs"] is None
    assert ev["files"] == []


def test_the_watcher_publishes_when_a_file_appears_and_nothing_after_it_stops(tmp_path):
    got: list[dict] = []
    seen = threading.Event()

    def publish(payload: dict) -> None:
        got.append(payload)
        seen.set()

    watcher = WorkspaceWatcher(publish=publish)
    watcher.set_workspace(str(tmp_path))
    try:
        time.sleep(0.5)  # the watch is up before the first write
        (tmp_path / "hello.txt").write_text("hi\n")
        assert seen.wait(5), "no workspace_changed within 5 s of the write"
        assert got[0]["dirs"] == [""]
        assert got[0]["files"] == ["hello.txt"]

        watcher.set_workspace(None)
        (tmp_path / "after.txt").write_text("late\n")
        time.sleep(1.5)
        assert len(got) == 1
    finally:
        watcher.stop()


def test_pointing_at_the_same_folder_again_keeps_the_running_watch(tmp_path):
    watcher = WorkspaceWatcher(publish=lambda _p: None)
    watcher.set_workspace(str(tmp_path))
    try:
        first = watcher._thread
        watcher.set_workspace(str(tmp_path))
        assert watcher._thread is first
        assert watcher.root == str(tmp_path.resolve())
    finally:
        watcher.stop()
    assert watcher.root is None


def test_the_default_publisher_puts_the_event_on_the_app_wide_channel(tmp_path):
    """A wildcard subscriber of the session-events bus (what /api/sessions/events
    reads) receives the watcher's event with the empty, app-wide session id."""
    from server.agents.event_bus import event_bus

    async def main():
        q = event_bus.subscribe_all()
        watcher = WorkspaceWatcher()
        watcher.set_workspace(str(tmp_path))
        try:
            await asyncio.sleep(0.5)
            (tmp_path / "live.txt").write_text("x\n")
            return await asyncio.wait_for(q.get(), timeout=5)
        finally:
            watcher.stop()
            event_bus.unsubscribe_all(q)

    sid, ev = asyncio.run(main())
    assert sid == ""
    assert ev["type"] == "workspace_changed"
    assert ev["dirs"] == [""]
    assert ev["files"] == ["live.txt"]
