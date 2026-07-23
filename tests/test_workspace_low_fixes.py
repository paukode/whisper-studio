"""Regression tests for four low-severity audit fixes.

(1) Agent register/emit report the RESOLVED model, not config.model (None).
(2) _resolve_type_globs shells out to the resolved rg binary, not bare "rg".
(3) _atomic_write_text uses a unique temp file per call (no per-pid collision),
    so concurrent writes to the same path cannot corrupt each other.
(4) ws_create_worktree validates the worktree name before it becomes a path
    segment / branch component.
"""

import asyncio
import json
import os
import subprocess
import threading

from server.search import grep
from server.workspace import _atomic_write_text, executors, paths

# ---------------------------------------------------------------------------
# (3) Atomic write — unique temp names + no corruption under concurrency
# ---------------------------------------------------------------------------


def test_atomic_write_normal_content(tmp_path):
    """A plain write still lands the exact content."""
    target = str(tmp_path / "out.txt")
    _atomic_write_text(target, "correct content")
    assert open(target).read() == "correct content"


def test_atomic_write_uses_unique_mkstemp_names(tmp_path, monkeypatch):
    """Each write allocates a distinct temp file (mkstemp), not a constant
    `.name.tmp.<pid>` that two concurrent writers would share."""
    target = str(tmp_path / "shared.txt")
    created = []
    real_mkstemp = paths.tempfile.mkstemp

    def spy(*a, **k):
        fd, path = real_mkstemp(*a, **k)
        created.append(path)
        return fd, path

    monkeypatch.setattr(paths.tempfile, "mkstemp", spy)

    _atomic_write_text(target, "one")
    _atomic_write_text(target, "two")

    assert len(created) == 2
    # Distinct temp files — the core of the fix.
    assert created[0] != created[1]
    for p in created:
        base = os.path.basename(p)
        assert base.startswith(".shared.txt.")
        assert base.endswith(".tmp")
        # Sibling of the destination (same dir) so os.replace stays atomic.
        assert os.path.dirname(p) == str(tmp_path)
    # Final content is the last writer's payload; nothing left behind.
    assert open(target).read() == "two"
    assert os.listdir(tmp_path) == ["shared.txt"]


def test_atomic_write_concurrent_no_corruption(tmp_path):
    """Many threads writing distinct large payloads to one path must never
    yield a mixed/partial file — the destination equals exactly one payload."""
    target = str(tmp_path / "race.txt")
    # Large, distinct payloads so an interleaved/partial write is detectable.
    payloads = [f"{i}:" + (chr(ord("a") + i) * 200_000) for i in range(8)]
    barrier = threading.Barrier(len(payloads))
    errors: list[Exception] = []

    def writer(payload):
        try:
            barrier.wait()
            _atomic_write_text(target, payload)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    final = open(target).read()
    assert final in payloads  # a complete payload, never a blend
    # No temp files survived the race.
    assert os.listdir(tmp_path) == ["race.txt"]


def test_atomic_write_overwrite_preserves_mode(tmp_path):
    """Overwriting keeps the destination's existing permission bits."""
    target = str(tmp_path / "perm.txt")
    with open(target, "w") as f:
        f.write("orig")
    os.chmod(target, 0o640)
    _atomic_write_text(target, "new")
    assert open(target).read() == "new"
    import stat

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640


# ---------------------------------------------------------------------------
# (4) ws_create_worktree — name validation
# ---------------------------------------------------------------------------


def _git_workspace(tmp_path, monkeypatch):
    os.makedirs(tmp_path / ".git")
    monkeypatch.setattr(executors, "get_workspace_path", lambda: str(tmp_path))


def test_create_worktree_rejects_unsafe_names(tmp_path, monkeypatch):
    _git_workspace(tmp_path, monkeypatch)
    # Ensure a rejection never reaches subprocess.
    monkeypatch.setattr(
        executors.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess must not run")),
    )
    for bad in ["../evil", "a;b", "a b", "-flag", "a/../b", "foo`id`", "$(whoami)", "a|b"]:
        r = executors._exec_ws_create_worktree({"name": bad}, "", None)
        assert "Invalid worktree name" in r, f"expected rejection for {bad!r}, got: {r}"


def test_create_worktree_accepts_clean_slug(tmp_path, monkeypatch):
    _git_workspace(tmp_path, monkeypatch)
    calls = {}

    class FakeResult:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, *a, **k):
        calls["cmd"] = cmd
        return FakeResult()

    monkeypatch.setattr(executors.subprocess, "run", fake_run)

    r = executors._exec_ws_create_worktree({"name": "feature-x"}, "", None)
    data = json.loads(r)
    assert data["created"] is True
    assert data["name"] == "feature-x"
    assert data["branch"] == "whisper/feature-x"
    # git actually invoked with the validated branch name.
    assert calls["cmd"][0] == "git"
    assert "whisper/feature-x" in calls["cmd"]


# ---------------------------------------------------------------------------
# (2) _resolve_type_globs — resolved rg binary
# ---------------------------------------------------------------------------


def test_resolve_type_globs_uses_resolved_rg_path(monkeypatch):
    import server.search.engine as engine

    monkeypatch.setattr(engine, "get_rg_path", lambda: "/custom/bin/rg")
    captured = {}

    class FakeResult:
        stdout = "py: *.py, *.pyi\n"

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_run)

    globs = grep._resolve_type_globs("py")

    assert captured["cmd"][0] == "/custom/bin/rg"  # not bare "rg"
    assert "--type-list" in captured["cmd"]
    assert globs == ["*.py", "*.pyi"]


# ---------------------------------------------------------------------------
# (1) Agent register/emit carry the resolved model, not None
# ---------------------------------------------------------------------------


def test_agent_registers_and_emits_resolved_model(monkeypatch):
    import server.agents.tools as agent_tools
    import server.chat as chat
    import server.workspace as workspace
    from server.agents.event_bus import event_bus
    from server.agents.registry import agent_registry
    from server.agents.runtime import run_agent

    RESOLVED = "us.anthropic.claude-test-v1:0"

    reg_calls: list[dict] = []
    monkeypatch.setattr(agent_registry, "register", lambda *a, **k: reg_calls.append(k))
    monkeypatch.setattr(agent_registry, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(agent_registry, "cleanup_completed", lambda *a, **k: None)

    events: list[dict] = []
    monkeypatch.setattr(event_bus, "publish", lambda ch, ev: events.append(ev))

    # Stub the heavy loop dependencies so no real tools/bedrock are needed.
    monkeypatch.setattr(chat, "assemble_tool_pool", lambda **k: [])
    monkeypatch.setattr(workspace, "get_workspace_path", lambda: "")
    monkeypatch.setattr(agent_tools, "get_agent_runtime_tools", lambda *a, **k: [])

    invoked = {}

    class FakeBody:
        def read(self):
            return json.dumps(
                {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}
            ).encode()

    class FakeBedrock:
        def invoke_model(self, **kwargs):
            invoked["modelId"] = kwargs.get("modelId")
            return {"body": FakeBody()}

    monkeypatch.setattr(chat, "_get_bedrock_client", lambda: FakeBedrock())

    result = asyncio.run(
        run_agent(
            "do a thing",
            agent_type="general",
            session_id="sess-low-fix",
            model_id_override=RESOLVED,
        )
    )

    assert result.status == "completed"
    # register() got the resolved model, not config.model (None).
    assert reg_calls and reg_calls[0]["model"] == RESOLVED
    # The 'started' progress event carries the resolved model, not None.
    started = [e for e in events if e.get("phase") == "started"]
    assert started and started[0]["model"] == RESOLVED
    # And the bedrock call actually used it.
    assert invoked.get("modelId") == RESOLVED
