"""The 'save_to_path' approval action — server/approval/bootstrap.py's
_do_save_to_path, registered as its own category ("save") so it does not
inherit "Yes for all writes" session-memory and does not trigger the
write/delete/cli git-changes cache invalidation (these files live outside
any workspace).

Unlike _do_write/_do_create, this executor must NEVER require a connected
workspace — get_workspace_path() is not consulted at all.
"""

import asyncio
import base64
import os

from server.approval import registry
from server.approval.bootstrap import _do_save_to_path, register_defaults

register_defaults()


def _run(coro):
    return asyncio.run(coro)


def test_save_to_path_is_registered_with_its_own_category():
    spec = registry.get("save_to_path")
    assert spec is not None
    assert spec.category == "save"
    assert spec.preview == "text"


def test_writes_utf8_content_to_absolute_path(tmp_path):
    dest = str(tmp_path / "report.txt")
    outcome = _run(
        _do_save_to_path(
            {
                "path": dest,
                "content": "hello world",
                "content_encoding": "utf-8",
                "filename": "report.txt",
            }
        )
    )
    assert outcome.ok is True
    assert open(dest, encoding="utf-8").read() == "hello world"


def test_writes_base64_binary_content(tmp_path):
    dest = str(tmp_path / "image.png")
    raw = b"\x89PNG\r\n\x1a\nbinarydata"
    outcome = _run(
        _do_save_to_path(
            {
                "path": dest,
                "content": base64.b64encode(raw).decode("ascii"),
                "content_encoding": "base64",
                "filename": "image.png",
            }
        )
    )
    assert outcome.ok is True
    with open(dest, "rb") as f:
        assert f.read() == raw


def test_records_the_directory_as_a_save_location(tmp_path, monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "server.workspace.state.record_save_location",
        lambda p: recorded.append(p),
    )
    dest = str(tmp_path / "notes.txt")
    _run(_do_save_to_path({"path": dest, "content": "hi", "filename": "notes.txt"}))
    assert recorded == [str(tmp_path)]


def test_rejects_relative_path():
    outcome = _run(_do_save_to_path({"path": "relative.txt", "content": "hi"}))
    assert outcome.ok is False
    assert "absolute" in (outcome.error or "").lower()


def test_rejects_empty_path():
    outcome = _run(_do_save_to_path({"path": "", "content": "hi"}))
    assert outcome.ok is False


def test_rejects_sensitive_path(tmp_path, monkeypatch):
    # Point the denylist at a throwaway tmp_path directory instead of a real
    # user path (e.g. ~/.ssh) — this must never risk touching a real
    # developer's actual credentials regardless of whether the check works.
    fake_denied = tmp_path / "denied"
    fake_denied.mkdir()
    monkeypatch.setattr(
        "server.security.sensitive_paths.expanded_sandbox_paths",
        lambda: [str(fake_denied)],
    )
    dest = str(fake_denied / "secret.txt")
    outcome = _run(_do_save_to_path({"path": dest, "content": "should not be written"}))
    assert outcome.ok is False
    assert "sensitive" in (outcome.error or "").lower()
    assert not os.path.exists(dest)


def test_never_requires_a_connected_workspace(tmp_path, monkeypatch):
    def _boom():
        raise AssertionError("save_to_path must not call get_workspace_path")

    monkeypatch.setattr("server.workspace.get_workspace_path", _boom)
    dest = str(tmp_path / "no_workspace_needed.txt")
    outcome = _run(_do_save_to_path({"path": dest, "content": "hi"}))
    assert outcome.ok is True


def test_bad_base64_content_fails_cleanly(tmp_path):
    dest = str(tmp_path / "bad.bin")
    outcome = _run(
        _do_save_to_path(
            {
                "path": dest,
                "content": "not-valid-base64!!!",
                "content_encoding": "base64",
            }
        )
    )
    assert outcome.ok is False
    assert not os.path.exists(dest)
