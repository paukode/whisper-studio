"""Cancellable /api/local-model/load.

The composer's model select opens this SSE to drive the loading banner. It
used to be fire-and-forget: the download ran in-thread (unkillable — deleting
the model in Settings left it running and the weights came back), and the
banner had no Cancel. Now the download phase runs as a models-manager job
(the same killable worker Settings drives), a cancel endpoint flags the
stream, and the stream reports stage ``cancelled`` instead of loading.
"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.chat.routes as routes_mod
from server.local import runtime, serving
from server.models_manager import catalog, manager


def _client():
    app = FastAPI()
    app.include_router(routes_mod.router)
    return TestClient(app)


def _frames(resp_text: str) -> list[dict]:
    out = []
    for ln in resp_text.splitlines():
        if ln.startswith("data: ") and ln != "data: [DONE]":
            out.append(json.loads(ln[len("data: ") :]))
    return out


def _stub_model(monkeypatch, downloaded: bool):
    monkeypatch.setattr(runtime, "is_local_model", lambda k: k == "local_test")
    monkeypatch.setattr(runtime, "is_downloaded", lambda k: downloaded)
    monkeypatch.setattr(runtime, "local_model_meta", lambda k: {"label": "Test Model"})
    monkeypatch.setattr(runtime, "set_requested_n_ctx", lambda n: None)


class _Entry:
    key = "local_test"
    repo_id = "fake/repo"


def test_cancel_endpoint_flags_model_and_kills_download_job(monkeypatch):
    _stub_model(monkeypatch, downloaded=False)
    cancelled: list[str] = []
    monkeypatch.setattr(catalog, "get_entry", lambda k: _Entry())
    monkeypatch.setattr(manager, "cancel", lambda e: cancelled.append(e.key) or "cancelled")

    resp = _client().post("/api/local-model/cancel-load?model=local_test")
    assert resp.status_code == 200 and resp.json() == {"cancelling": True}
    assert cancelled == ["local_test"]
    with routes_mod._load_cancels_lock:
        assert "local_test" in routes_mod._load_cancels
        routes_mod._load_cancels.discard("local_test")


def test_cancel_endpoint_tolerates_no_running_download(monkeypatch):
    _stub_model(monkeypatch, downloaded=True)
    monkeypatch.setattr(catalog, "get_entry", lambda k: _Entry())

    def _no_job(e):
        raise manager.Conflict("This model is not downloading or queued.")

    monkeypatch.setattr(manager, "cancel", _no_job)
    resp = _client().post("/api/local-model/cancel-load?model=local_test")
    assert resp.status_code == 200
    with routes_mod._load_cancels_lock:
        routes_mod._load_cancels.discard("local_test")


def test_load_download_phase_reports_cancelled(monkeypatch):
    """A cancel flag raised mid-download ends the stream with stage
    'cancelled' — no load, no error toast material."""
    _stub_model(monkeypatch, downloaded=False)
    monkeypatch.setattr(catalog, "get_entry", lambda k: _Entry())
    monkeypatch.setattr(manager, "start_download", lambda e: "started")
    monkeypatch.setattr(manager, "reap", lambda: None)
    monkeypatch.setattr(manager, "state_of", lambda e: ("downloading", None))
    monkeypatch.setattr(manager, "progress_of", lambda e, s=None: 0.4)
    # The flag is normally set by the cancel endpoint while the stream runs;
    # forcing the check true avoids sleeping through real poll ticks.
    monkeypatch.setattr(routes_mod, "_load_cancel_requested", lambda m: True)
    ensure_calls: list[str] = []
    monkeypatch.setattr(serving, "ensure_serving", lambda k, n=None, **kw: ensure_calls.append(k))

    resp = _client().get("/api/local-model/load?model=local_test")
    frames = _frames(resp.text)
    assert frames[-1]["stage"] == "cancelled"
    assert ensure_calls == []  # never proceeded to the memory-load phase


def test_load_download_phase_reports_settings_cancel_as_cancelled(monkeypatch):
    """Cancelling from Settings > Models kills the shared manager job; the
    load stream sees state 'absent' and ends as cancelled instead of spinning
    (the old in-thread download survived deletes and kept the banner alive)."""
    _stub_model(monkeypatch, downloaded=False)
    monkeypatch.setattr(catalog, "get_entry", lambda k: _Entry())
    monkeypatch.setattr(manager, "start_download", lambda e: "started")
    monkeypatch.setattr(manager, "reap", lambda: None)
    monkeypatch.setattr(manager, "state_of", lambda e: ("absent", None))

    resp = _client().get("/api/local-model/load?model=local_test")
    frames = _frames(resp.text)
    assert frames[-1]["stage"] == "cancelled"


def test_load_memory_phase_cancel_stops_this_models_server(monkeypatch):
    """A cancel during warm-up stops the server only when it is (becoming)
    this model, and the stream reports cancelled even though ensure_serving
    completed."""
    import time as _time

    _stub_model(monkeypatch, downloaded=True)
    monkeypatch.setattr(catalog, "get_entry", lambda k: _Entry())
    stops: list[str] = []
    monkeypatch.setattr(
        serving, "ensure_serving", lambda k, n=None, **kw: _time.sleep(0.9) or "http://x"
    )
    monkeypatch.setattr(serving, "resident_key", lambda: "local_test")
    monkeypatch.setattr(serving, "stop", lambda: stops.append("stop"))
    monkeypatch.setattr(routes_mod, "_load_cancel_requested", lambda m: True)

    resp = _client().get("/api/local-model/load?model=local_test")
    frames = _frames(resp.text)
    assert frames[-1]["stage"] == "cancelled"
    assert stops  # the warming server was stopped


def test_load_memory_phase_cancel_spares_a_superseding_model(monkeypatch):
    """If another model already took the slot by the time the cancel lands,
    its fresh server is NOT collateral damage."""
    import time as _time

    _stub_model(monkeypatch, downloaded=True)
    monkeypatch.setattr(catalog, "get_entry", lambda k: _Entry())
    stops: list[str] = []
    monkeypatch.setattr(
        serving, "ensure_serving", lambda k, n=None, **kw: _time.sleep(0.9) or "http://x"
    )
    monkeypatch.setattr(serving, "resident_key", lambda: "some_other_model")
    monkeypatch.setattr(serving, "stop", lambda: stops.append("stop"))
    monkeypatch.setattr(routes_mod, "_load_cancel_requested", lambda m: True)

    resp = _client().get("/api/local-model/load?model=local_test")
    frames = _frames(resp.text)
    assert frames[-1]["stage"] == "cancelled"
    assert stops == []


def test_load_happy_path_downloads_via_manager_then_ready(monkeypatch):
    """No cancel: a missing download runs as a manager job, then the memory
    load proceeds to 'ready'."""
    state = {"installed": False}
    monkeypatch.setattr(runtime, "is_local_model", lambda k: k == "local_test")
    monkeypatch.setattr(runtime, "is_downloaded", lambda k: state["installed"])
    monkeypatch.setattr(runtime, "local_model_meta", lambda k: {"label": "Test Model"})
    monkeypatch.setattr(runtime, "set_requested_n_ctx", lambda n: None)
    monkeypatch.setattr(catalog, "get_entry", lambda k: _Entry())

    def _start(e):
        state["installed"] = True  # instant download
        return "started"

    monkeypatch.setattr(manager, "start_download", _start)
    monkeypatch.setattr(manager, "reap", lambda: None)
    monkeypatch.setattr(serving, "ensure_serving", lambda k, n=None, **kw: "http://x")

    resp = _client().get("/api/local-model/load?model=local_test")
    frames = _frames(resp.text)
    assert frames[-1]["stage"] == "ready" and frames[-1]["progress"] == 1.0
