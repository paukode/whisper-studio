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
