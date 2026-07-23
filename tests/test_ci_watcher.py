"""WS-J slice 1: CI provider parsing + the watcher poll loop, with a FAKE gh
(no network). Exercises run/job normalization, terminal detection, the
resolve-retry for a just-pushed branch, cancellation, and the max-polls ceiling.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from server.ci import provider, watcher


def _cp(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode=returncode, stdout=stdout, stderr=stderr)


# ── provider ───────────────────────────────────────────────────────────────
def test_list_runs_normalizes(monkeypatch):
    payload = (
        '[{"databaseId":123,"status":"completed","conclusion":"failure",'
        '"workflowName":"CI","headBranch":"feat/x","headSha":"abc","url":"u",'
        '"event":"push","createdAt":"t"}]'
    )
    monkeypatch.setattr(provider, "_run_gh", lambda *a, **k: _cp(payload))
    runs = provider.list_runs("feat/x", "/repo")
    assert runs == [
        {
            "run_id": 123,
            "status": "completed",
            "conclusion": "failure",
            "workflow": "CI",
            "branch": "feat/x",
            "sha": "abc",
            "url": "u",
            "event": "push",
            "created_at": "t",
        }
    ]
    assert provider.is_failing(runs[0]) and provider.is_terminal(runs[0])


def test_get_run_includes_failed_jobs(monkeypatch):
    payload = (
        '{"databaseId":5,"status":"completed","conclusion":"failure",'
        '"workflowName":"CI","headBranch":"b","headSha":"s","url":"u","event":"push",'
        '"createdAt":"t","jobs":[{"name":"Backend","status":"completed","conclusion":"failure",'
        '"url":"j1"},{"name":"Frontend","status":"completed","conclusion":"success","url":"j2"}]}'
    )
    monkeypatch.setattr(provider, "_run_gh", lambda *a, **k: _cp(payload))
    run = provider.get_run(5, "/repo")
    failed = provider.failed_jobs(run)
    assert [j["name"] for j in failed] == ["Backend"]


def test_provider_degrades_on_gh_error(monkeypatch):
    monkeypatch.setattr(provider, "_run_gh", lambda *a, **k: _cp("", returncode=1, stderr="boom"))
    assert provider.list_runs("b", "/repo") == []
    assert provider.get_run(1, "/repo") is None
    assert provider.pr_for_branch("b", "/repo") is None


def test_failing_log_tail_sliced(monkeypatch):
    monkeypatch.setattr(provider, "_run_gh", lambda *a, **k: _cp("X" * 50_000))
    out = provider.failing_log(9, "/repo", max_bytes=1000)
    assert len(out) < 1100 and out.startswith("…(head elided)…")


# ── watcher ────────────────────────────────────────────────────────────────
def _no_sleep():
    async def _s(_):
        return None

    return _s


def test_watch_polls_until_terminal(monkeypatch):
    states = [
        {"run_id": 1, "status": "in_progress", "conclusion": None, "jobs": []},
        {"run_id": 1, "status": "in_progress", "conclusion": None, "jobs": []},
        {
            "run_id": 1,
            "status": "completed",
            "conclusion": "failure",
            "url": "u",
            "jobs": [{"name": "Backend", "conclusion": "failure"}],
        },
    ]
    seq = iter(states)
    monkeypatch.setattr(provider, "latest_run", lambda *a, **k: states[0])
    monkeypatch.setattr(provider, "get_run", lambda *a, **k: next(seq))

    events = []
    out = asyncio.run(
        watcher.watch_branch(
            "feat/x", cwd="/repo", on_event=events.append, sleep=_no_sleep(), max_polls=10
        )
    )
    assert out["status"] == "completed" and out["conclusion"] == "failure"
    assert out["failing"] is True
    assert [j["name"] for j in out["failed_jobs"]] == ["Backend"]
    assert sum(1 for e in events if e["type"] == "ci_progress") == 3


def test_watch_no_run_after_retries(monkeypatch):
    monkeypatch.setattr(provider, "latest_run", lambda *a, **k: None)
    out = asyncio.run(watcher.watch_branch("feat/x", cwd="/repo", sleep=_no_sleep()))
    assert out["found"] is False and out["run_id"] is None


def test_watch_cancellation(monkeypatch):
    monkeypatch.setattr(provider, "latest_run", lambda *a, **k: {"run_id": 1, "status": "queued"})
    monkeypatch.setattr(
        provider, "get_run", lambda *a, **k: {"run_id": 1, "status": "in_progress", "jobs": []}
    )
    ev = asyncio.Event()
    ev.set()
    out = asyncio.run(
        watcher.watch_branch("feat/x", cwd="/repo", cancel_event=ev, sleep=_no_sleep())
    )
    assert out["cancelled"] is True


def test_watch_max_polls_times_out(monkeypatch):
    monkeypatch.setattr(provider, "latest_run", lambda *a, **k: {"run_id": 1, "status": "queued"})
    monkeypatch.setattr(
        provider, "get_run", lambda *a, **k: {"run_id": 1, "status": "in_progress", "jobs": []}
    )
    out = asyncio.run(watcher.watch_branch("feat/x", cwd="/repo", sleep=_no_sleep(), max_polls=3))
    assert out["timed_out"] is True


def test_registry_accepts_ci_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path))
    from server.tasks import registry

    tid = registry.create_task("ci", title="watch feat/x", session_id="s1")
    assert tid
    task = registry.get_task(tid)
    assert task and task["kind"] == "ci"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
