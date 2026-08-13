"""The suite must never touch the running checkout's real app database.

Eight modules compute ``<storage_root()>/sessions.db`` at import time, so any
test that writes a session, background task, cost row, cron run, attachment or
notification used to land it in whatever checkout ran the suite. That is how a
workflow run — newly mirrored into the background-task registry — started
leaving phantom "Workflow: noop" tasks in a developer's own app.

conftest's ``_isolate_app_databases`` redirects all of them per test. These
tests pin that guarantee, so a newly added module with the same import-time
pattern fails here instead of quietly writing to real data.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from tests.conftest import _DB_MODULES


def _real_db() -> str:
    from server.infrastructure.paths import storage_root

    return os.path.join(storage_root(), "sessions.db")


@pytest.mark.parametrize("module_name", _DB_MODULES)
def test_module_db_path_is_redirected_away_from_the_checkout(module_name):
    import importlib

    mod = importlib.import_module(module_name)
    for attr in ("DB_PATH", "_DB_PATH"):
        if hasattr(mod, attr):
            assert getattr(mod, attr) != _real_db(), f"{module_name}.{attr} points at real app data"


def test_every_db_module_shares_the_one_test_database():
    """One tmp db per test, not one per module — cross-module reads (a route
    writing a session and a tracker reading it) have to keep working."""
    import importlib

    paths = set()
    for name in _DB_MODULES:
        mod = importlib.import_module(name)
        for attr in ("DB_PATH", "_DB_PATH"):
            if hasattr(mod, attr):
                paths.add(getattr(mod, attr))
    assert len(paths) == 1


def test_writing_a_background_task_does_not_reach_the_real_database():
    """The end-to-end guarantee, not just the path value."""
    from server.tasks import registry

    registry.create_task("shell", session_id="s1", title="isolation-canary", command="true")

    real = _real_db()
    if not os.path.exists(real):
        return  # nothing to leak into
    with sqlite3.connect(real) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "agent_tasks" not in names:
            return
        hits = conn.execute(
            "SELECT count(*) FROM agent_tasks WHERE title=?", ("isolation-canary",)
        ).fetchone()[0]
    assert hits == 0, "test data reached the running checkout's real sessions.db"
