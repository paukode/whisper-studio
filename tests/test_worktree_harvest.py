"""Agent worktree harvest: all work (committed + uncommitted) comes home
uncommitted, per-file, with clean/partial/kept/conflict handling.

Regression coverage for the review findings:
- committed work must NOT be lost (base-commit diff, not HEAD/status),
- one overlapping file must not block the rest (per-file partial apply),
- identical content is a no-op, not a conflict,
- turn/deadline-limited (stopped_early) agents keep their worktree,
- the "removed" note must reflect reality.
"""

import pathlib
import subprocess

import pytest

from server.git.worktree_harvest import harvest_agent_worktree
from server.git.worktree_session import enter_worktree, get_session


def _git(args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-b", "feat/thing"], root)
    _git(["config", "user.email", "t@t"], root)
    _git(["config", "user.name", "t"], root)
    (root / "base.txt").write_text("base\n")
    (root / ".gitignore").write_text(".whisper/\n")  # worktrees are gitignored
    _git(["add", "-A"], root)
    _git(["commit", "-m", "base"], root)
    return str(root)


def _spawn(repo, agent_id="ag1"):
    return enter_worktree(repo, f"agent-{agent_id}", f"agent:{agent_id}")


def _harvest(repo, s, apply=True):
    return harvest_agent_worktree(
        repo,
        s.worktree_path,
        s.worktree_branch,
        f"agent:{s.worktree_name.split('agent-')[-1]}",
        apply,
        base_commit=s.original_head_commit,
    )


def _status(repo):
    return _git(["status", "--porcelain"], repo)


def test_uncommitted_work_applied_and_cleaned(repo):
    s = _spawn(repo)
    wt = pathlib.Path(s.worktree_path)
    (wt / "new_module.py").write_text("print('hi')\n")
    (wt / "base.txt").write_text("base\nchanged\n")

    out = _harvest(repo, s)
    assert out["status"] == "applied"
    assert out["files"] == 2
    st = _status(repo)
    assert "?? new_module.py" in st
    assert "M base.txt" in st
    assert "agent" not in _git(["log", "--oneline"], repo)  # nothing committed
    assert not wt.exists()
    assert "worktree-agent-ag1" not in _git(["branch"], repo)
    assert get_session("agent:ag1") is None
    assert "UNCOMMITTED" in out["note"] and "feat/thing" in out["note"]


def test_committed_work_is_not_lost(repo):
    """The critical case: an agent that COMMITS its work leaves a clean working
    tree; the harvest must still bring the committed delta home, not treat the
    worktree as empty and delete the branch."""
    s = _spawn(repo, "agc")
    wt = pathlib.Path(s.worktree_path)
    (wt / "committed.py").write_text("x = 1\n")
    (wt / "base.txt").write_text("base\ncommitted change\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-m", "agent committed"], wt)
    # working tree is now clean in the worktree
    assert _git(["status", "--porcelain"], wt).strip() == ""

    out = _harvest(repo, s)
    assert out["status"] == "applied"
    assert out["files"] == 2
    assert (pathlib.Path(repo) / "committed.py").read_text() == "x = 1\n"
    assert "committed change" in (pathlib.Path(repo) / "base.txt").read_text()
    # Applied UNCOMMITTED in the main tree.
    assert "?? committed.py" in _status(repo)
    assert "committed" not in _git(["log", "--oneline"], repo)
    assert not wt.exists()


def test_mixed_committed_and_uncommitted(repo):
    s = _spawn(repo, "agmix")
    wt = pathlib.Path(s.worktree_path)
    (wt / "a.py").write_text("a\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-m", "commit a"], wt)
    (wt / "b.py").write_text("b\n")  # uncommitted new file
    out = _harvest(repo, s)
    assert out["status"] == "applied"
    assert (pathlib.Path(repo) / "a.py").exists()
    assert (pathlib.Path(repo) / "b.py").exists()


def test_partial_apply_one_conflict_others_land(repo):
    """One file conflicting with a local edit must not strand the agent's other
    (non-conflicting) files."""
    s = _spawn(repo, "agp")
    wt = pathlib.Path(s.worktree_path)
    (wt / "base.txt").write_text("base\nagent edit\n")  # will conflict
    (wt / "fresh.py").write_text("fresh\n")  # brand new, no conflict
    # user edits base.txt differently in the main tree (uncommitted)
    (pathlib.Path(repo) / "base.txt").write_text("base\nuser edit\n")

    out = _harvest(repo, s)
    assert out["status"] == "partial"
    assert out["files"] == 1  # fresh.py applied
    assert out["conflicts"] == 1
    assert (pathlib.Path(repo) / "fresh.py").read_text() == "fresh\n"
    # user's version of the conflicting file is untouched
    assert (pathlib.Path(repo) / "base.txt").read_text() == "base\nuser edit\n"
    # worktree kept for recovery of the conflicting file
    assert wt.exists()
    assert "base.txt" in out["note"] and s.worktree_path in out["note"]


def test_identical_change_is_noop_not_conflict(repo):
    s = _spawn(repo, "agi")
    (pathlib.Path(s.worktree_path) / "base.txt").write_text("base\nsame\n")
    # user already made the identical change
    (pathlib.Path(repo) / "base.txt").write_text("base\nsame\n")
    out = _harvest(repo, s)
    # nothing to apply, no conflict -> treated as applied(0)/clean, worktree gone
    assert out["status"] == "applied"
    assert out["conflicts"] == 0
    assert not pathlib.Path(s.worktree_path).exists()


def test_stopped_early_agent_keeps_worktree(repo):
    s = _spawn(repo, "agse")
    (pathlib.Path(s.worktree_path) / "partial.txt").write_text("wip\n")
    out = _harvest(repo, s, apply=False)
    assert out["status"] == "kept"
    assert "did not finish" in out["note"]
    assert pathlib.Path(s.worktree_path).exists()
    assert _status(repo).strip() == ""  # main tree untouched


def test_clean_worktree_removed(repo):
    s = _spawn(repo, "agcl")
    out = _harvest(repo, s)
    assert out["status"] == "clean"
    assert not pathlib.Path(s.worktree_path).exists()


def test_deleted_file_propagates(repo):
    s = _spawn(repo, "agd")
    (pathlib.Path(s.worktree_path) / "base.txt").unlink()
    out = _harvest(repo, s)
    assert out["status"] == "applied"
    assert not (pathlib.Path(repo) / "base.txt").exists()
    assert "D base.txt" in _status(repo)


def test_executable_bit_preserved(repo):
    import os

    s = _spawn(repo, "agx")
    script = pathlib.Path(s.worktree_path) / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    os.chmod(script, 0o755)
    _git(["add", "-A"], pathlib.Path(s.worktree_path))  # git records the mode
    out = _harvest(repo, s)
    assert out["status"] == "applied"
    applied = pathlib.Path(repo) / "run.sh"
    assert applied.exists()
    assert os.stat(applied).st_mode & 0o111  # executable


def test_symlink_preserved_as_symlink(repo):
    s = _spawn(repo, "agln")
    link = pathlib.Path(s.worktree_path) / "link.txt"
    link.symlink_to("base.txt")
    _git(["add", "-A"], pathlib.Path(s.worktree_path))
    out = _harvest(repo, s)
    assert out["status"] == "applied"
    applied = pathlib.Path(repo) / "link.txt"
    assert applied.is_symlink()
    import os

    assert os.readlink(applied) == "base.txt"


def test_exec_bit_cleared(repo):
    import os

    # base has an executable script; agent de-escalates it to 0644, same content.
    (pathlib.Path(repo) / "run.sh").write_text("#!/bin/sh\necho hi\n")
    os.chmod(pathlib.Path(repo) / "run.sh", 0o755)
    _git(["add", "-A"], repo)
    _git(["update-index", "--chmod=+x", "run.sh"], repo)
    _git(["commit", "-m", "add script"], repo)

    s = _spawn(repo, "agec")
    wt_script = pathlib.Path(s.worktree_path) / "run.sh"
    os.chmod(wt_script, 0o644)
    _git(["add", "-A"], pathlib.Path(s.worktree_path))
    out = _harvest(repo, s)
    assert out["status"] == "applied"
    assert not (os.stat(pathlib.Path(repo) / "run.sh").st_mode & 0o111)  # exec bit cleared


def test_symlink_conflict_not_clobbered(repo):
    """A user's local symlink at a path the agent changed must be detected as a
    conflict (git hash-object follows symlinks, which used to defeat this)."""
    import os

    s = _spawn(repo, "aglc")
    # agent turns base.txt into a symlink to elsewhere
    (pathlib.Path(s.worktree_path) / "base.txt").unlink()
    os.symlink("agent_target", pathlib.Path(s.worktree_path) / "base.txt")
    _git(["add", "-A"], pathlib.Path(s.worktree_path))
    # user independently made base.txt a DIFFERENT symlink in the main tree
    (pathlib.Path(repo) / "base.txt").unlink()
    os.symlink("user_target", pathlib.Path(repo) / "base.txt")

    out = _harvest(repo, s)
    assert out["status"] == "kept"
    assert out["conflicts"] == 1
    # user's symlink is intact, not clobbered into a regular file
    assert os.path.islink(pathlib.Path(repo) / "base.txt")
    assert os.readlink(pathlib.Path(repo) / "base.txt") == "user_target"


def test_tracked_directory_replaced_by_file(repo):
    """A TRACKED directory in the main tree, swapped by the agent for a file of
    the same name: deletions-first empties it, then os.rmdir + write succeeds."""
    import shutil as _sh

    # base gains a tracked dir thing/ (present in the main tree)
    (pathlib.Path(repo) / "thing").mkdir()
    (pathlib.Path(repo) / "thing" / "a.txt").write_text("a\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "add tracked dir"], repo)

    s = _spawn(repo, "agdf")
    wt = pathlib.Path(s.worktree_path)
    _sh.rmtree(wt / "thing")
    (wt / "thing").write_text("now a file\n")

    out = _harvest(repo, s)
    assert out["status"] == "applied"
    assert (pathlib.Path(repo) / "thing").is_file()
    assert (pathlib.Path(repo) / "thing").read_text() == "now a file\n"


def test_untracked_directory_not_clobbered(repo):
    """CRITICAL: an agent adding a file whose name collides with the user's
    UNTRACKED directory must NOT rmtree that directory. The dir (and its files)
    is preserved and the collision is reported as a conflict."""
    s = _spawn(repo, "agud")
    (pathlib.Path(s.worktree_path) / "notes").write_text("agent file\n")
    # user has an untracked directory of the same name holding local work
    (pathlib.Path(repo) / "notes").mkdir()
    (pathlib.Path(repo) / "notes" / "todo.txt").write_text("precious\n")

    out = _harvest(repo, s)
    assert out["status"] in ("kept", "partial")
    assert out["conflicts"] == 1
    # user's directory and its contents survive untouched
    assert (pathlib.Path(repo) / "notes").is_dir()
    assert (pathlib.Path(repo) / "notes" / "todo.txt").read_text() == "precious\n"
    # worktree kept so the agent's file is recoverable
    assert pathlib.Path(s.worktree_path).exists()


def test_is_null_sha_handles_both_hash_lengths():
    from server.git.worktree_harvest import _is_null_sha

    assert _is_null_sha("0" * 40) is True
    assert _is_null_sha("0" * 64) is True  # sha256
    assert _is_null_sha("") is True
    assert _is_null_sha("df967b9") is False


def test_missing_worktree_dir_is_clean(repo):
    out = harvest_agent_worktree(
        repo, repo + "/.whisper/worktrees/agent-gone", "worktree-agent-gone", "agent:gone", True
    )
    assert out["status"] == "clean"


def test_run_agent_applies_only_on_genuine_finish(monkeypatch, repo):
    """run_agent harvests with apply=True on a clean finish, apply=False when
    the result is stopped_early (turn/deadline limit)."""
    import asyncio
    from types import SimpleNamespace

    from server.agents import runtime as rt

    fake_session = SimpleNamespace(
        original_cwd=repo,
        worktree_path=repo + "/.whisper/worktrees/agent-x",
        worktree_branch="worktree-agent-x",
        original_head_commit="deadbeef",
    )
    monkeypatch.setattr(rt, "_enter_agent_worktree", lambda a, s: fake_session)
    seen = {}

    def _fake_harvest(root, path, branch, key, apply, base_commit=None):
        seen["apply"] = apply
        seen["base_commit"] = base_commit
        return {"status": "applied", "files": 1, "conflicts": 0, "note": "n"}

    monkeypatch.setattr("server.git.worktree_harvest.harvest_agent_worktree", _fake_harvest)

    async def _finish(**kw):
        return rt.AgentResult(agent_id="x", agent_type="general", output="ok", status="completed")

    monkeypatch.setattr(rt, "_run_agent_loop", _finish)
    asyncio.run(rt.run_agent("t", session_id="", model_id_override="m", isolation="worktree"))
    assert seen["apply"] is True
    assert seen["base_commit"] == "deadbeef"

    async def _limited(**kw):
        return rt.AgentResult(
            agent_id="x",
            agent_type="general",
            output="ran out",
            status="completed",
            stopped_early=True,
        )

    monkeypatch.setattr(rt, "_run_agent_loop", _limited)
    asyncio.run(rt.run_agent("t", session_id="", model_id_override="m", isolation="worktree"))
    assert seen["apply"] is False  # stopped_early -> keep the worktree
