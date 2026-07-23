"""WS-J follow-ups: failing-log secret scrub + per-run watch re-attach."""

from __future__ import annotations

import json

import pytest

from server.ci import manager, provider


# ── secret scrub (defense-in-depth over GitHub's own masking) ──────────────
def test_scrub_redacts_common_secrets():
    raw = (
        "aws key AKIAIOSFODNN7EXAMPLE failed\n"
        "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n"
        "Authorization: Bearer eyJhbGciOiJexamplelongtoken12345\n"
        "export API_KEY=supersecretvalue123\n"
        "slack xoxb-1234567890-abcdefghij\n"
    )
    out = provider.scrub_secrets(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in out
    assert "supersecretvalue123" not in out
    assert "xoxb-1234567890-abcdefghij" not in out
    assert "REDACTED" in out
    # the key NAME is kept for context; only the value is redacted
    assert "API_KEY" in out and "GITHUB_TOKEN" in out


def test_scrub_keeps_ordinary_log_text():
    raw = "AssertionError: expected 1 but got 2\nTypeError: 'NoneType' is not subscriptable\n"
    assert provider.scrub_secrets(raw) == raw


def test_failing_log_is_scrubbed(monkeypatch):
    import subprocess

    cp = subprocess.CompletedProcess([], 0, stdout="token AKIAIOSFODNN7EXAMPLE here", stderr="")
    monkeypatch.setattr(provider, "_run_gh", lambda *a, **k: cp)
    out = provider.failing_log(1, "/repo")
    assert "AKIAIOSFODNN7EXAMPLE" not in out and "REDACTED" in out


# ── per-run re-attach (survives reload; keyed by task id, not branch) ──────
def test_finish_stores_full_payload_and_get_watch_reconstructs(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path))
    from server.tasks import registry

    tid = registry.create_task("ci", title="CI watch feat/x", meta={"branch": "feat/x"})
    monkeypatch.setattr(manager, "_emit_result", lambda *a, **k: None)
    monkeypatch.setattr(manager, "_emit_task_event", lambda *a, **k: None)

    outcome = {
        "run_id": 42,
        "status": "completed",
        "conclusion": "failure",
        "failing": True,
        "url": "https://gh/run/42",
        "failed_jobs": [{"name": "Backend"}],
        "timed_out": False,
        "cancelled": False,
    }
    manager._finish(tid, "s1", "feat/x", outcome)

    state = manager.get_watch(tid)
    assert state is not None
    # The WATCH completed (it followed the run to a conclusion); the run's own
    # conclusion is 'failure' — two different things.
    assert state["terminal"] is True and state["status"] == "completed"
    assert state["branch"] == "feat/x"
    run = state["run"]
    assert run["run_id"] == 42 and run["conclusion"] == "failure"
    assert run["failing"] is True and run["failed_jobs"] == ["Backend"]
    assert run["url"] == "https://gh/run/42"
    # the stored result_text is valid JSON carrying the task status
    row = registry.get_task(tid)
    assert json.loads(row["result_text"])["task_status"] == "completed"


def test_get_watch_running_before_finish(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path))
    from server.tasks import registry

    tid = registry.create_task("ci", title="CI watch b", meta={"branch": "b"})
    state = manager.get_watch(tid)
    assert state["terminal"] is False and state["run"] is None and state["branch"] == "b"


def test_get_watch_unknown_or_wrong_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path))
    from server.tasks import registry

    assert manager.get_watch("nope") is None
    tid = registry.create_task("shell", title="not ci")
    assert manager.get_watch(tid) is None


def test_route_watch_state(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.ci.routes import router
    from server.tasks import registry

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    assert client.get("/api/ci/watch/missing").status_code == 404
    tid = registry.create_task("ci", title="CI watch b", meta={"branch": "b"})
    r = client.get(f"/api/ci/watch/{tid}")
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == tid and body["branch"] == "b" and body["terminal"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
