"""GET /api/workspace/pick-save-target — the one-shot native save-file dialog
backing the save_file tool's location picker. Same shape/contract as the
existing /pick-folder endpoint (mocked subprocess.run, same {'path', 'cancelled'}
response, same 501 fallback on non-Darwin), plus filename sanitization since
`filename` is a query param (user/model-influenced) reaching an osascript
command string.
"""

import subprocess
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.workspace.routes.browse as browse
from server.workspace import router as ws_router


def _client():
    app = FastAPI()
    app.include_router(ws_router)
    return TestClient(app)


def test_returns_path_on_success(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="/Users/me/Documents/report.pdf\n", stderr="")

    monkeypatch.setattr(browse.subprocess, "run", fake_run)

    resp = _client().get("/api/workspace/pick-save-target", params={"filename": "report.pdf"})
    assert resp.status_code == 200
    assert resp.json() == {"path": "/Users/me/Documents/report.pdf"}

    # Built as a Python list (never a shell string) — mirrors pick-folder.
    args = captured["args"]
    assert isinstance(args, list)
    assert args[0] == "osascript"
    assert "report.pdf" in args[2]
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["timeout"] == 120


def test_returns_cancelled_on_empty_stdout(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        browse.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    resp = _client().get("/api/workspace/pick-save-target", params={"filename": "x.txt"})
    assert resp.json() == {"path": None, "cancelled": True}


def test_returns_cancelled_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        browse.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="User canceled"),
    )
    resp = _client().get("/api/workspace/pick-save-target", params={"filename": "x.txt"})
    assert resp.json() == {"path": None, "cancelled": True}


def test_returns_cancelled_on_timeout(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=120)

    monkeypatch.setattr(browse.subprocess, "run", fake_run)
    resp = _client().get("/api/workspace/pick-save-target", params={"filename": "x.txt"})
    assert resp.json() == {"path": None, "cancelled": True}


def test_501_on_non_darwin(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    resp = _client().get("/api/workspace/pick-save-target", params={"filename": "x.txt"})
    assert resp.status_code == 501


def test_strips_embedded_double_quotes_from_filename(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    captured = {}

    def fake_run(args, **kwargs):
        captured["script"] = args[2]
        return SimpleNamespace(returncode=0, stdout="/tmp/x\n", stderr="")

    monkeypatch.setattr(browse.subprocess, "run", fake_run)

    malicious = 'evil" & do shell script "rm -rf ~" & "'
    _client().get("/api/workspace/pick-save-target", params={"filename": malicious})

    script = captured["script"]
    # The AppleScript string literal must not contain an embedded, unescaped
    # double quote from the untrusted filename — that would let the
    # attacker's text escape the quoted `default name "..."` literal.
    inner = script.split('default name "', 1)[1].rsplit('"', 1)[0]
    assert '"' not in inner
    assert "\\" not in inner


def test_strips_embedded_backslashes_from_filename(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    captured = {}

    def fake_run(args, **kwargs):
        captured["script"] = args[2]
        return SimpleNamespace(returncode=0, stdout="/tmp/x\n", stderr="")

    monkeypatch.setattr(browse.subprocess, "run", fake_run)

    _client().get("/api/workspace/pick-save-target", params={"filename": "a\\b\\c.txt"})
    assert "\\" not in captured["script"].split('default name "', 1)[1]


def test_empty_filename_falls_back_to_a_default_name(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    captured = {}

    def fake_run(args, **kwargs):
        captured["script"] = args[2]
        return SimpleNamespace(returncode=0, stdout="/tmp/x\n", stderr="")

    monkeypatch.setattr(browse.subprocess, "run", fake_run)

    _client().get("/api/workspace/pick-save-target", params={"filename": ""})
    assert 'default name ""' not in captured["script"]
