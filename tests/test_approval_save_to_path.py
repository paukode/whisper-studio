"""The 'save_to_path' approval action — server/approval/bootstrap.py's
_do_save_to_path, registered as its own category ("save") so it does not
inherit "Yes for all writes" session-memory and does not trigger the
write/delete/cli git-changes cache invalidation (these files live outside
any workspace).

Contract: the file-saving tools build and STAGE the finished bytes in the
app sandbox at tool-call time (server/workspace/paths.py's
stage_file_bytes); approving here just MOVES the staged file to the
destination. The staged_path-must-be-inside-the-staging-root check is
security-critical: the payload can in principle be forged via a direct
POST /api/approval/execute, and without the gate a crafted staged_path
could 'move' any readable file on disk into a user-visible folder.

Unlike _do_write/_do_create, this executor must NEVER require a connected
workspace — get_workspace_path() is not consulted at all.
"""

import asyncio
import os

from server.approval import registry
from server.approval.bootstrap import _do_save_to_path, register_defaults
from server.workspace.paths import stage_file_bytes

register_defaults()


def _run(coro):
    return asyncio.run(coro)


def test_save_to_path_is_registered_with_its_own_category():
    spec = registry.get("save_to_path")
    assert spec is not None
    assert spec.category == "save"
    assert spec.preview == "text"


def test_moves_staged_file_to_destination(tmp_path):
    staged = stage_file_bytes(b"hello world", "report.txt")
    dest = str(tmp_path / "report.txt")
    outcome = _run(
        _do_save_to_path({"path": dest, "filename": "report.txt", "staged_path": staged})
    )
    assert outcome.ok is True
    with open(dest, "rb") as f:
        assert f.read() == b"hello world"
    assert not os.path.exists(staged), "staged copy must be moved, not duplicated"
    assert dest in (outcome.output or "")


def test_moves_binary_bytes_intact(tmp_path):
    raw = b"\x89PNG\r\n\x1a\nbinarydata"
    staged = stage_file_bytes(raw, "image.png")
    dest = str(tmp_path / "image.png")
    outcome = _run(_do_save_to_path({"path": dest, "filename": "image.png", "staged_path": staged}))
    assert outcome.ok is True
    with open(dest, "rb") as f:
        assert f.read() == raw


def test_creates_missing_parent_directories(tmp_path):
    staged = stage_file_bytes(b"nested", "deep.txt")
    dest = str(tmp_path / "a" / "b" / "deep.txt")
    outcome = _run(_do_save_to_path({"path": dest, "staged_path": staged}))
    assert outcome.ok is True
    assert open(dest, encoding="utf-8").read() == "nested"


def test_records_the_directory_as_a_save_location(tmp_path, monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "server.workspace.state.record_save_location",
        lambda p: recorded.append(p),
    )
    staged = stage_file_bytes(b"hi", "notes.txt")
    dest = str(tmp_path / "notes.txt")
    _run(_do_save_to_path({"path": dest, "staged_path": staged}))
    assert recorded == [str(tmp_path)]


def test_rejects_staged_path_outside_staging_root(tmp_path):
    # Forged payload pointing at an arbitrary readable file must be refused,
    # or a direct POST /api/approval/execute could exfiltrate any file on
    # disk by "moving" it somewhere user-visible.
    victim = tmp_path / "victim.txt"
    victim.write_text("secret")
    dest = str(tmp_path / "out.txt")
    outcome = _run(_do_save_to_path({"path": dest, "staged_path": str(victim)}))
    assert outcome.ok is False
    assert "staged" in (outcome.error or "").lower()
    assert victim.exists(), "the non-staged file must not be moved"
    assert not os.path.exists(dest)


def test_rejects_missing_staged_file(tmp_path):
    staged = stage_file_bytes(b"temp", "gone.txt")
    os.remove(staged)
    dest = str(tmp_path / "gone.txt")
    outcome = _run(_do_save_to_path({"path": dest, "staged_path": staged}))
    assert outcome.ok is False
    assert "no longer exists" in (outcome.error or "")


def test_rejects_relative_path():
    outcome = _run(_do_save_to_path({"path": "relative.txt", "staged_path": ""}))
    assert outcome.ok is False
    assert "absolute" in (outcome.error or "").lower()


def test_rejects_empty_path():
    outcome = _run(_do_save_to_path({"path": "", "staged_path": ""}))
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
    staged = stage_file_bytes(b"should not land", "secret.txt")
    dest = str(fake_denied / "secret.txt")
    outcome = _run(_do_save_to_path({"path": dest, "staged_path": staged}))
    assert outcome.ok is False
    assert "sensitive" in (outcome.error or "").lower()
    assert not os.path.exists(dest)
    assert os.path.exists(staged), "staged file must stay in the sandbox on refusal"


def test_never_requires_a_connected_workspace(tmp_path, monkeypatch):
    def _boom():
        raise AssertionError("save_to_path must not call get_workspace_path")

    monkeypatch.setattr("server.workspace.get_workspace_path", _boom)
    staged = stage_file_bytes(b"hi", "no_workspace_needed.txt")
    dest = str(tmp_path / "no_workspace_needed.txt")
    outcome = _run(_do_save_to_path({"path": dest, "staged_path": staged}))
    assert outcome.ok is True
