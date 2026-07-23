"""The terminal _sessions dict is guarded by a threading.Lock, and
_reap_dead_sessions (run on every terminal_create) cleans up dead sessions via
kill() (no raw os.close/del that could KeyError or double-close)."""

import threading

import server.terminal as T

_LockType = type(threading.Lock())


class FakeSession:
    def __init__(self, sid, *, alive=True, hidden=False):
        self.id = sid
        self.cwd = "/tmp"
        self.cols = 80
        self.rows = 24
        self.hidden = hidden
        self._alive = alive
        self.killed = 0

    @property
    def alive(self):
        return self._alive

    def kill(self):
        self.killed += 1


def test_sessions_lock_is_threading_lock():
    assert isinstance(T._sessions_lock, _LockType)


def test_reap_removes_dead_keeps_live(monkeypatch):
    monkeypatch.setattr(T, "_sessions", {})
    live = FakeSession("live", alive=True, hidden=False)
    dead = FakeSession("dead", alive=False, hidden=False)
    hidden = FakeSession("hidden", alive=True, hidden=True)
    T._sessions.update({"live": live, "dead": dead, "hidden": hidden})

    T._reap_dead_sessions()

    # Dead session was removed and killed exactly once (no double-close path).
    assert "dead" not in T._sessions
    assert dead.killed == 1
    # Live + hidden sessions remain and were not killed.
    assert set(T._sessions) == {"live", "hidden"}
    assert live.killed == 0 and hidden.killed == 0
