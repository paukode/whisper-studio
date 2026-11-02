"""An unreadable buddy.json is never re-hatched over: GET /api/buddy answers
with an error that names the file, and a missing file still means "not hatched
yet"."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from server import buddy


def test_corrupt_buddy_file_is_an_error_with_the_path(tmp_path, monkeypatch):
    path = tmp_path / "buddy.json"
    path.write_text("{not json")
    monkeypatch.setattr(buddy, "BUDDY_CONFIG_PATH", str(path))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(buddy.get_buddy())

    assert exc_info.value.status_code == 500
    assert str(path) in exc_info.value.detail
    assert path.read_text() == "{not json"  # left for the user to inspect


def test_missing_buddy_file_means_not_hatched(tmp_path, monkeypatch):
    monkeypatch.setattr(buddy, "BUDDY_CONFIG_PATH", str(tmp_path / "buddy.json"))

    out = asyncio.run(buddy.get_buddy())

    assert out["hatched"] is False
