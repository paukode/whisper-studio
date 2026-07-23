"""_lock_for must hand out a single shared lock per session id (atomic
setdefault), so concurrent writers actually serialize on the same object."""

import asyncio

from server.infrastructure import sessions as S


def test_lock_for_returns_stable_identity(monkeypatch):
    monkeypatch.setattr(S, "_session_locks", {})

    a = S._lock_for("sess-1")
    b = S._lock_for("sess-1")
    assert a is b, "repeated calls for one id must return the SAME lock"
    assert isinstance(a, asyncio.Lock)

    c = S._lock_for("sess-2")
    assert c is not a, "different ids get different locks"
    assert set(S._session_locks) == {"sess-1", "sess-2"}
