"""Switching workspaces must drop state scoped to the old workspace.

`connect_workspace` used to clear only WORKSPACE_BACKUPS. The per-session cwd
(`server.cwd_tracker`) and the worktree registry (`_WORKTREES` in
`server.workspace.executors`) survived the switch, so after moving to a new
workspace read-only commands kept running in the OLD folder and worktree ops
resolved stale branch names. These tests pin that a real switch resets both,
while the initial connect / reconnect to the same path leaves them alone.
"""

import os

import server.cwd_tracker as cwd_tracker
import server.workspace.executors as executors
import server.workspace.state as state
from server.cwd_tracker import get_cwd, update_cwd
from server.workspace.state import connect_workspace


def _isolate_config(monkeypatch, tmp_path):
    """Point config + recents at tmp so connect_workspace never touches real state."""
    monkeypatch.setattr(state, "WORKSPACE_CONFIG_PATH", str(tmp_path / "workspace.json"))
    monkeypatch.setattr(state, "RECENT_WORKSPACES_PATH", str(tmp_path / "recent.json"))


def _reset_registries():
    cwd_tracker._session_cwd.clear()
    executors._WORKTREES.clear()


def test_switch_clears_session_cwd_and_worktrees(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _reset_registries()

    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    ws_a.mkdir()
    ws_b.mkdir()

    # Initial connect to A.
    real_a = connect_workspace(str(ws_a))

    # Seed per-session cwd (pointing into A) and a worktree registry entry.
    update_cwd("sess1", real_a)
    executors._WORKTREES["wt1"] = {"path": str(ws_a / ".worktrees/wt1"), "branch": "whisper/wt1"}
    assert get_cwd("sess1", str(ws_b)) == real_a  # persisted before the switch

    # Switch to B — an actual workspace change.
    real_b = connect_workspace(str(ws_b))
    assert real_b != real_a

    # Per-session cwd is cleared: get_cwd falls back to the passed default
    # (the new workspace), NOT the stale A path.
    assert get_cwd("sess1", real_b) == real_b
    assert get_cwd("sess1", real_b) != real_a
    # Worktree registry is empty.
    assert executors._WORKTREES == {}


def test_reconnect_same_path_keeps_state(monkeypatch, tmp_path):
    # Reconnecting to the SAME workspace is not a switch — live session state
    # (cwd within the workspace, worktrees) must survive.
    _isolate_config(monkeypatch, tmp_path)
    _reset_registries()

    ws = tmp_path / "ws"
    ws.mkdir()
    real = connect_workspace(str(ws))

    sub = ws / "src"
    sub.mkdir()
    update_cwd("sess1", str(sub))
    executors._WORKTREES["wt1"] = {"path": str(ws / ".worktrees/wt1"), "branch": "whisper/wt1"}

    # Reconnect to the same realpath.
    connect_workspace(str(ws))
    assert get_cwd("sess1", real) == str(sub)  # unchanged
    assert executors._WORKTREES == {
        "wt1": {"path": str(ws / ".worktrees/wt1"), "branch": "whisper/wt1"}
    }


def test_initial_connect_does_not_crash_with_no_prior_config(monkeypatch, tmp_path):
    # First-ever connect (no config file, previous path is None) must not be
    # treated as a switch and must not raise.
    _isolate_config(monkeypatch, tmp_path)
    _reset_registries()

    ws = tmp_path / "ws"
    ws.mkdir()
    # Seed state that a spurious reset would wipe; initial connect must leave it.
    update_cwd("sess1", str(ws))
    executors._WORKTREES["wt1"] = {"path": "x", "branch": "y"}

    real = connect_workspace(str(ws))
    assert real == os.path.realpath(str(ws))
    # No switch happened (previous was None) → seeded state survives.
    assert get_cwd("sess1", str(ws)) == str(ws)
    assert executors._WORKTREES == {"wt1": {"path": "x", "branch": "y"}}
