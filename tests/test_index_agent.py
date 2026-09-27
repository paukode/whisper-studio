"""Background index-refresh launchd agent: wake-interval/plist generation, the
per-workspace cadence due-check, app-running detection, and the skip-when-running
guard. None of these touch launchctl or run a real index build."""

import os

from server.index import agent


def test_wake_intervals_union_of_opted_in_hours(monkeypatch):
    monkeypatch.setattr(agent, "_opted_in_workspaces", lambda: ["/a", "/b", "/c"])
    settings = {
        "/a": {"schedule": {"hour": 7}},
        "/b": {"schedule": {"hour": 22}},
        "/c": {"schedule": {"hour": 7}},  # duplicate hour collapses
    }
    monkeypatch.setattr(agent.wssettings, "get_settings", lambda ws: settings[ws])
    assert agent._wake_intervals() == [{"Hour": 7, "Minute": 0}, {"Hour": 22, "Minute": 0}]


def test_plist_dict_runs_worker_via_venv():
    p = agent._plist_dict([{"Hour": 7, "Minute": 0}])
    assert p["Label"] == agent._LABEL
    assert p["ProgramArguments"][1:] == ["-m", "server.index.agent", "run"]
    assert p["StartCalendarInterval"] == [{"Hour": 7, "Minute": 0}]
    assert p["RunAtLoad"] is False


def test_due_check_per_cadence():
    now = 1_000_000.0
    assert agent._due({"frequency": "daily"}, 0, now) is True  # never run
    assert agent._due({"frequency": "daily"}, now - 1000, now) is False  # just ran
    assert agent._due({"frequency": "daily"}, now - 21 * 3600, now) is True  # a day later
    assert agent._due({"frequency": "every_n_days", "interval_days": 3}, now - 1000, now) is False
    assert (
        agent._due({"frequency": "every_n_days", "interval_days": 3}, now - 3 * 86400, now) is True
    )
    assert (
        agent._due({"frequency": "weekly", "weekday": "mon"}, now - 8 * 86400, now) is True
    )  # catch-up


def test_app_running_detection(monkeypatch, tmp_path):
    pid_file = tmp_path / ".server.pid"
    monkeypatch.setattr(agent, "_PID_PATH", str(pid_file))
    assert agent._app_running() is False  # no file

    pid_file.write_text(str(os.getpid()))  # our own live PID
    assert agent._app_running() is True

    pid_file.write_text("99999999")  # not a live process
    assert agent._app_running() is False


def test_run_skips_when_app_is_running(monkeypatch):
    monkeypatch.setattr(agent, "_app_running", lambda: True)

    def boom():
        raise AssertionError("must not enumerate workspaces when the app is running")

    monkeypatch.setattr(agent.store, "list_indexed_workspaces", boom)
    assert agent.run() == 0  # returns before iterating


def _database_before_the_cost_migrations(monkeypatch, path) -> None:
    """A sessions.db as an install left it before migrations 021 and 022:
    session_costs in its old shape (no source or provenance columns)."""
    import sqlite3

    from server.infrastructure import sessions
    from server.migrations import runner

    monkeypatch.setattr(sessions, "DB_PATH", path)
    sessions._ensure_db()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    runner._ensure_schema_version_table(conn)
    for m in runner._discover_migrations():
        if m["version"] >= 21:
            continue
        m["migrate"](conn)
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (m["version"], m["description"]),
        )
    conn.commit()
    conn.close()


def test_a_refresh_before_the_app_ever_migrated_still_logs_its_paid_calls(monkeypatch, tmp_path):
    # After an update launchd runs the NEW code, possibly before the app has
    # launched once. The refresh's cost rows must land, not fail on the old
    # table shape.
    import sqlite3

    import server.index.pipeline as pipeline
    from server.costs import tracker
    from server.migrations import runner

    db = tmp_path / "sessions.db"
    _database_before_the_cost_migrations(monkeypatch, str(db))
    for mod in (runner, tracker):
        monkeypatch.setattr(mod, "DB_PATH", str(db))
    monkeypatch.setattr(runner, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(tracker, "_ensured", set())

    monkeypatch.setattr(agent, "_app_running", lambda: False)
    monkeypatch.setattr(agent.store, "list_indexed_workspaces", lambda: ["/ws"])
    monkeypatch.setattr(agent.store, "has_index", lambda ws: True)
    monkeypatch.setattr(agent.store, "get_meta", lambda ws: {})
    monkeypatch.setattr(agent.store, "set_meta", lambda ws, **kw: None)
    monkeypatch.setattr(
        agent.wssettings,
        "get_settings",
        lambda ws: {"refresh_when_closed": True, "schedule": {"enabled": True}},
    )

    def build(ws):
        # What an embed call of the refresh logs.
        tracker.record_turn("", 0, "cohere.embed-v4:0", 1200, 0, source="index")

    monkeypatch.setattr(pipeline, "build", build)

    assert agent.run() == 0
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT model, input_tokens, source FROM session_costs").fetchall()
    conn.close()
    assert rows == [("cohere.embed-v4:0", 1200, "index")]


def test_a_failed_migration_refreshes_nothing(monkeypatch):
    import server.migrations.runner as runner

    def broken():
        raise RuntimeError("disk full")

    monkeypatch.setattr(agent, "_app_running", lambda: False)
    monkeypatch.setattr(runner, "run_migrations", broken)

    def boom():
        raise AssertionError("must not refresh on an unmigrated database")

    monkeypatch.setattr(agent.store, "list_indexed_workspaces", boom)
    assert agent.run() == 1
