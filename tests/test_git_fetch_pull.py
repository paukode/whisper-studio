"""git_fetch and git_pull: the two remote-sync operations the assistant had no
tool for. The command sandbox has no git credentials, so a shelled fetch fails
with a permission error; these run outside it against a real bare remote."""

import json
import subprocess

import pytest

from server.git import executor as gx
from server.git.tools import GIT_READ_TOOLS, GIT_WRITE_TOOLS


def _git(repo, *args, capture=False):
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.stdout.strip() if capture else ""


@pytest.fixture
def remote_and_clones(tmp_path, monkeypatch):
    """A bare remote, the workspace clone the tools act on, and a second clone
    that plays the other machine pushing new commits."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--initial-branch=main")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main")
    _git(seed, "config", "user.email", "tester@example.com")
    _git(seed, "config", "user.name", "Tester")
    (seed / "base.txt").write_text("base\n")
    _git(seed, "add", "base.txt")
    _git(seed, "commit", "-m", "base commit")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "main")

    workspace = tmp_path / "workspace"
    _git(tmp_path, "clone", str(bare), str(workspace))
    _git(workspace, "config", "user.email", "tester@example.com")
    _git(workspace, "config", "user.name", "Tester")
    monkeypatch.setattr(gx, "get_workspace_path", lambda: str(workspace))
    return bare, workspace, seed


def _push_from_other_machine(seed, name: str) -> str:
    (seed / name).write_text(f"{name}\n")
    _git(seed, "add", name)
    _git(seed, "commit", "-m", f"add {name}")
    _git(seed, "push", "origin", "main")
    return _git(seed, "rev-parse", "HEAD", capture=True)


def test_fetch_updates_remote_tracking_branch_without_touching_files(remote_and_clones):
    _bare, workspace, seed = remote_and_clones
    new_head = _push_from_other_machine(seed, "second.txt")
    assert _git(workspace, "rev-parse", "origin/main", capture=True) != new_head

    out = gx._exec_git_fetch({"__session_id__": "s"}, "", None)

    assert out.startswith("Fetched origin")
    assert _git(workspace, "rev-parse", "origin/main", capture=True) == new_head
    assert not (workspace / "second.txt").exists()  # fetch never changes the tree


def test_fetch_rejects_an_unsafe_remote_name(remote_and_clones):
    out = gx._exec_git_fetch({"remote": "origin; rm -rf /"}, "", None)
    assert out.startswith("Error: invalid remote name")


def test_pull_is_approval_gated_and_fast_forwards(remote_and_clones):
    _bare, workspace, seed = remote_and_clones
    new_head = _push_from_other_machine(seed, "second.txt")

    gated = gx._exec_git_pull({"__session_id__": "s1", "remote": "origin"}, "", None)
    assert gated.startswith("[WS_APPROVAL]")
    payload = json.loads(gated[len("[WS_APPROVAL]") :])
    assert payload == {
        "action": "git_pull",
        "remote": "origin",
        "branch": "",
        "rebase": False,
        "session_id": "s1",
    }

    ok, out = gx.do_git_pull(payload)
    assert ok, out
    assert _git(workspace, "rev-parse", "HEAD", capture=True) == new_head
    assert (workspace / "second.txt").exists()


def test_pull_refuses_a_diverged_branch_and_points_at_rebase(remote_and_clones):
    _bare, workspace, seed = remote_and_clones
    _push_from_other_machine(seed, "theirs.txt")
    (workspace / "mine.txt").write_text("mine\n")
    _git(workspace, "add", "mine.txt")
    _git(workspace, "commit", "-m", "local work")

    ok, out = gx.do_git_pull({"remote": "origin", "branch": "main", "rebase": False})
    assert not ok
    assert "rebase=true" in out

    ok, out = gx.do_git_pull({"remote": "origin", "branch": "main", "rebase": True})
    assert ok, out
    assert (workspace / "theirs.txt").exists() and (workspace / "mine.txt").exists()


def test_tools_are_advertised_and_pull_has_an_approval_spec():
    assert "git_fetch" in {t["name"] for t in GIT_READ_TOOLS}
    assert "git_pull" in {t["name"] for t in GIT_WRITE_TOOLS}
    from server.approval import bootstrap, registry

    if registry.get("git_pull") is None:
        bootstrap.register_defaults()
    spec = registry.get("git_pull")
    assert spec is not None and spec.category == "cli"
    assert "git pull --ff-only origin main" == spec.render_command(
        {"remote": "origin", "branch": "main"}
    )
