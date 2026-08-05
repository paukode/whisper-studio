"""Model download manager (/api/models/*): catalog shape, installed detection
against a throwaway WHISPER_HOME, download job lifecycle (queueing beyond the
concurrency cap, benign duplicate starts, cancel/dequeue), delete rules, and
the status poll endpoint.

No network anywhere: the worker subprocess is replaced by a fake process and
the HF size API by a canned fetch. The catalog's static entries are derived
from the real backend modules (numpy imports, no model loads), so this suite
also pins the sentinel layout against drift.
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.models_manager import manager, sizes
from server.models_manager.catalog import get_entry
from server.models_manager.routes import router

STATIC_KEYS = {
    "whisper",
    "parakeet",
    "ecapa_speaker",
    "qwen3_embed",
    "qwen3_rerank",
    "gliner",
    "gliner2",
}
# Built-in local registry models exist regardless of config.json contents.
BUILTIN_LOCAL_KEYS = {"local_gemma", "local_gemma_coder"}

ENTRY_FIELDS = {
    "key",
    "label",
    "group",
    "repo_id",
    "installed",
    "size_bytes_estimate",
    "bytes_on_disk",
    "progress",
    "state",
    "queue_position",
    "error",
}


class FakeProc:
    """Stands in for the download worker subprocess."""

    def __init__(self):
        self.rc = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.rc

    def terminate(self):
        self.terminated = True
        self.rc = -15

    def kill(self):  # pragma: no cover - only on stuck terminate
        self.killed = True
        self.rc = -9

    def wait(self, timeout=None):
        return self.rc


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Throwaway app home: models/ under tmp, clean job state, no network."""
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path))
    (tmp_path / "models").mkdir()
    monkeypatch.setattr(sizes, "fetch_size", lambda repo_id, filename=None: None)
    monkeypatch.setattr(sizes, "prefetch", lambda entries: None)
    manager._jobs.clear()
    manager._queue.clear()
    manager._errors.clear()
    manager._progress_max.clear()
    return tmp_path


@pytest.fixture()
def client(home):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture()
def fake_spawn(monkeypatch):
    """Replace the worker spawn; returns the dict of key -> FakeProc."""
    procs: dict[str, FakeProc] = {}

    def spawn(key, log_path):
        procs[key] = FakeProc()
        return procs[key]

    monkeypatch.setattr(manager, "_spawn_worker", spawn)
    return procs


def install_fake(home, key: str, nbytes: int = 2048) -> str:
    """Create the entry's real sentinel file structure under the tmp home."""
    entry = get_entry(key)
    assert entry is not None, f"no catalog entry for {key}"
    path = home / "models" / entry.dir_name / entry.sentinel_rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * nbytes)
    return str(path)


# ── catalog ──────────────────────────────────────────────────────────────


def test_catalog_shape_and_groups(client):
    models = client.get("/api/models/catalog").json()["models"]
    by_key = {m["key"]: m for m in models}
    assert STATIC_KEYS | BUILTIN_LOCAL_KEYS <= set(by_key)
    for m in models:
        assert ENTRY_FIELDS <= set(m), f"missing fields on {m['key']}"
        assert m["group"] in {"transcription", "indexing", "local-chat"}
    assert by_key["whisper"]["group"] == "transcription"
    assert by_key["parakeet"]["group"] == "transcription"
    assert by_key["ecapa_speaker"]["group"] == "transcription"
    for key in ("qwen3_embed", "qwen3_rerank", "gliner", "gliner2"):
        assert by_key[key]["group"] == "indexing"
    for key in BUILTIN_LOCAL_KEYS:
        assert by_key[key]["group"] == "local-chat"
        assert by_key[key]["repo_id"]
    # Fallback sizes answer while the HF fetch is stubbed out.
    assert by_key["whisper"]["size_bytes_estimate"] == sizes.FALLBACK_SIZES["whisper"]


def test_catalog_sentinels_match_backend_modules():
    """The catalog must check the same files the ensure functions download."""
    from server.index import config as ic

    assert get_entry("whisper").dir_name == "whisper-large-v3-turbo"
    assert get_entry("parakeet").dir_name == "parakeet-tdt-0.6b-v3"
    assert get_entry("ecapa_speaker").dir_name == "spkrec-ecapa-voxceleb"
    assert get_entry("qwen3_embed").sentinel_rel == os.path.relpath(
        ic.EMBED_SENTINEL, ic.EMBED_MODEL_DIR
    )
    assert get_entry("gliner").sentinel_rel == os.path.relpath(
        ic.GLINER_SENTINEL, ic.GLINER_MODEL_DIR
    )
    # GLiNER must go through the extractor's ensure (backbone tokenizer
    # pre-cache), never a bare snapshot_download.
    e = get_entry("gliner")
    assert (e.ensure_module, e.ensure_func) == ("server.index.extractor", "ensure_gliner_model")


def test_installed_detection_against_fake_home(client, home):
    install_fake(home, "whisper", nbytes=4096)
    install_fake(home, "local_gemma", nbytes=1024)
    by_key = {m["key"]: m for m in client.get("/api/models/catalog").json()["models"]}
    assert by_key["whisper"]["installed"] is True
    assert by_key["whisper"]["state"] == "installed"
    assert by_key["whisper"]["bytes_on_disk"] >= 4096
    assert by_key["local_gemma"]["installed"] is True
    assert by_key["local_gemma"]["bytes_on_disk"] >= 1024
    assert by_key["parakeet"]["installed"] is False
    assert by_key["parakeet"]["state"] == "absent"
    assert by_key["parakeet"]["bytes_on_disk"] == 0


# ── download lifecycle ───────────────────────────────────────────────────


def test_download_duplicate_start_is_benign(client, fake_spawn):
    first = client.post("/api/models/whisper/download")
    assert first.status_code == 200 and first.json()["started"] is True
    # A duplicate start is exactly what the requester wanted — 200, no-op.
    r = client.post("/api/models/whisper/download")
    assert r.status_code == 200
    body = r.json()
    assert body["started"] is False
    assert body["queued"] is False
    assert body["state"] == "downloading"
    # No second worker was spawned for the same key.
    assert len(fake_spawn) == 1


def test_download_409_when_installed(client, home, fake_spawn):
    install_fake(home, "parakeet")
    r = client.post("/api/models/parakeet/download")
    assert r.status_code == 409
    assert "installed" in r.json()["detail"].lower()


def test_download_beyond_cap_queues(client, fake_spawn):
    assert client.post("/api/models/whisper/download").json()["started"] is True
    assert client.post("/api/models/parakeet/download").json()["started"] is True
    # Third request: no error — it queues, with a 1-based position.
    r = client.post("/api/models/qwen3_embed/download")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "started": False,
        "queued": True,
        "key": "qwen3_embed",
        "state": "queued",
        "queue_position": 1,
    }
    assert "qwen3_embed" not in fake_spawn  # no worker yet
    st = client.get("/api/models/status").json()["models"]["qwen3_embed"]
    assert st["state"] == "queued"
    assert st["queue_position"] == 1
    cat = {m["key"]: m for m in client.get("/api/models/catalog").json()["models"]}
    assert cat["qwen3_embed"]["state"] == "queued"
    assert cat["qwen3_embed"]["queue_position"] == 1
    assert cat["qwen3_embed"]["installed"] is False


def test_queue_is_fifo_and_pumps_on_completion(client, fake_spawn):
    """Three queued keys start in request order as slots free up."""
    for key in ("whisper", "parakeet"):
        assert client.post(f"/api/models/{key}/download").json()["started"] is True
    order = ["qwen3_embed", "gliner", "ecapa_speaker"]
    for i, key in enumerate(order, start=1):
        body = client.post(f"/api/models/{key}/download").json()
        assert body["queued"] is True and body["queue_position"] == i

    # First slot frees: the OLDEST queued key starts; the others move up.
    fake_spawn["whisper"].rc = 0
    st = client.get("/api/models/status").json()["models"]
    assert st["qwen3_embed"]["state"] == "downloading"
    assert "qwen3_embed" in fake_spawn
    assert st["gliner"]["state"] == "queued" and st["gliner"]["queue_position"] == 1
    assert st["ecapa_speaker"]["state"] == "queued" and st["ecapa_speaker"]["queue_position"] == 2

    # Second slot frees: next in line starts.
    fake_spawn["parakeet"].rc = 0
    st = client.get("/api/models/status").json()["models"]
    assert st["gliner"]["state"] == "downloading"
    assert st["ecapa_speaker"]["state"] == "queued" and st["ecapa_speaker"]["queue_position"] == 1


def test_queue_pumps_on_status_poll(client, fake_spawn):
    """The manager is poll-driven: a queued item must start on the next
    status poll after a worker exits, with no other endpoint involved."""
    client.post("/api/models/whisper/download")
    client.post("/api/models/parakeet/download")
    client.post("/api/models/qwen3_embed/download")
    fake_spawn["whisper"].rc = 0
    # Nothing but the poll runs; the queued key must still start.
    st = client.get("/api/models/status").json()["models"]
    assert st["qwen3_embed"]["state"] == "downloading"
    assert st["qwen3_embed"]["queue_position"] is None


def test_queue_pumps_when_running_download_is_cancelled(client, fake_spawn):
    client.post("/api/models/whisper/download")
    client.post("/api/models/parakeet/download")
    client.post("/api/models/qwen3_embed/download")
    # Cancelling a RUNNING download frees its slot for the queued key at once.
    assert client.post("/api/models/whisper/cancel").status_code == 200
    assert "qwen3_embed" in fake_spawn
    st = client.get("/api/models/status").json()["models"]
    assert st["qwen3_embed"]["state"] == "downloading"


def test_duplicate_download_while_queued_is_benign(client, fake_spawn):
    client.post("/api/models/whisper/download")
    client.post("/api/models/parakeet/download")
    client.post("/api/models/qwen3_embed/download")
    r = client.post("/api/models/qwen3_embed/download")
    assert r.status_code == 200
    body = r.json()
    assert body["started"] is False
    assert body["queued"] is True
    assert body["queue_position"] == 1
    # Still exactly one queue slot — the duplicate didn't double-enqueue.
    fake_spawn["whisper"].rc = 0
    fake_spawn["parakeet"].rc = 0
    st = client.get("/api/models/status").json()["models"]
    assert st["qwen3_embed"]["state"] == "downloading"


def test_download_unknown_key_404(client):
    assert client.post("/api/models/nope/download").status_code == 404


def test_worker_failure_surfaces_error_state(client, fake_spawn):
    client.post("/api/models/gliner/download")
    fake_spawn["gliner"].rc = 1
    st = client.get("/api/models/status").json()["models"]["gliner"]
    assert st["state"] == "error"
    assert st["error"]


def test_completed_download_reports_installed(client, home, fake_spawn):
    client.post("/api/models/whisper/download")
    install_fake(home, "whisper")  # the worker's on-disk result
    fake_spawn["whisper"].rc = 0
    st = client.get("/api/models/status").json()["models"]["whisper"]
    assert st["state"] == "installed"
    assert st["error"] is None


# ── liveness + stall (a dead/hung worker must never stay "downloading") ──────


def test_killed_worker_becomes_error_not_stuck(client, fake_spawn, monkeypatch):
    """The core bug: a worker that dies WITHOUT poll() observing the exit must
    flip to 'error', not sit at 'downloading' forever. The PID liveness backstop
    (os.kill(pid, 0)) catches it even when poll() stays None."""
    client.post("/api/models/whisper/download")
    # poll() stays None (blind to the exit); the PID is gone.
    monkeypatch.setattr(manager, "_pid_alive", lambda pid: False)
    st = client.get("/api/models/status").json()["models"]["whisper"]
    assert st["state"] == "error"
    assert st["error"]
    # The freed slot is usable again — a retry starts cleanly.
    assert client.post("/api/models/whisper/download").json()["started"] is True


def test_worker_raises_surfaces_reason_and_exit_code(client, home, fake_spawn):
    """A worker that exits non-zero surfaces its ``DOWNLOAD FAILED:`` reason plus
    the exit code, not a bare traceback fragment."""
    client.post("/api/models/gliner/download")
    # The worker writes a traceback then its reason line, then exits 1.
    with open(manager._log_path("gliner"), "w") as f:
        f.write("Traceback (most recent call last):\n  ...\n")
        f.write("DOWNLOAD FAILED: repository not found on Hugging Face (check repo_id)\n")
    fake_spawn["gliner"].rc = 1
    st = client.get("/api/models/status").json()["models"]["gliner"]
    assert st["state"] == "error"
    assert "exit code 1" in st["error"]
    assert "repository not found" in st["error"]


def test_stall_no_progress_and_worker_gone_becomes_error(client, home, fake_spawn, monkeypatch):
    """No byte progress for the stall window AND the worker gone → error (rather
    than an eternal 0%). Uses a tiny window so the test doesn't wait 2 minutes."""
    monkeypatch.setattr(manager, "STALL_TIMEOUT_SEC", 60.0)
    client.post("/api/models/whisper/download")
    # First poll records a zero-byte baseline and arms the stall clock.
    client.get("/api/models/status")
    # No bytes ever land; the worker has vanished (poll blind, PID gone).
    monkeypatch.setattr(manager, "_pid_alive", lambda pid: False)
    with manager._lock:
        manager._jobs["whisper"].last_progress_at -= 120  # window elapsed
    st = client.get("/api/models/status").json()["models"]["whisper"]
    assert st["state"] == "error"
    assert st["error"]


def test_stall_alive_but_wedged_worker_is_terminated(client, home, fake_spawn, monkeypatch):
    """A worker still alive by PID but producing no bytes for the window is
    wedged: it is terminated (frees the slot) and reported as a stall — never
    left downloading forever. A progressing download resets the clock."""
    monkeypatch.setattr(manager, "STALL_TIMEOUT_SEC", 60.0)
    monkeypatch.setattr(manager, "_pid_alive", lambda pid: True)
    client.post("/api/models/whisper/download")
    client.get("/api/models/status")  # baseline, clock armed
    with manager._lock:
        manager._jobs["whisper"].last_progress_at -= 120  # no growth, window elapsed
    st = client.get("/api/models/status").json()["models"]["whisper"]
    assert st["state"] == "error"
    assert "stall" in st["error"].lower()
    assert fake_spawn["whisper"].terminated is True


def test_progressing_download_is_never_flagged_as_stalled(client, home, fake_spawn, monkeypatch):
    """Bytes still growing must keep the download alive even past the window —
    a slow-but-progressing transfer is not a stall."""
    monkeypatch.setattr(manager, "STALL_TIMEOUT_SEC", 0.0)  # any elapsed would trip a stall
    monkeypatch.setattr(manager, "_pid_alive", lambda pid: True)
    client.post("/api/models/parakeet/download")
    part = home / "models" / get_entry("parakeet").dir_name / "model.safetensors.incomplete"
    part.parent.mkdir(parents=True, exist_ok=True)
    for n in (1000, 2000, 3000):
        part.write_bytes(b"y" * n)  # growth on every poll
        st = client.get("/api/models/status").json()["models"]["parakeet"]
        assert st["state"] == "downloading"
    assert fake_spawn["parakeet"].terminated is False


def test_retry_after_error_restarts_cleanly(client, fake_spawn):
    """Retry (a fresh download POST) clears the dead job's error and starts a
    new worker — the user can recover a model stuck by a failed download."""
    client.post("/api/models/whisper/download")
    fake_spawn["whisper"].rc = 1
    assert client.get("/api/models/status").json()["models"]["whisper"]["state"] == "error"
    r = client.post("/api/models/whisper/download")
    assert r.status_code == 200 and r.json()["started"] is True
    st = client.get("/api/models/status").json()["models"]["whisper"]
    assert st["state"] == "downloading"
    assert st["error"] is None


def test_error_does_not_block_queue_pump(client, fake_spawn):
    """A failed running download frees its slot: the queued key must start on the
    same poll, exactly as a clean completion would."""
    client.post("/api/models/whisper/download")
    client.post("/api/models/parakeet/download")
    client.post("/api/models/qwen3_embed/download")  # queued (position 1)
    fake_spawn["whisper"].rc = 1  # running download errors out
    st = client.get("/api/models/status").json()["models"]
    assert st["whisper"]["state"] == "error"
    assert st["qwen3_embed"]["state"] == "downloading"
    assert "qwen3_embed" in fake_spawn  # the pump ran despite the error


# ── cancel ───────────────────────────────────────────────────────────────


def test_cancel_terminates_and_resets_state(client, fake_spawn):
    client.post("/api/models/whisper/download")
    r = client.post("/api/models/whisper/cancel")
    assert r.status_code == 200 and r.json()["cancelled"] is True
    assert fake_spawn["whisper"].terminated is True
    st = client.get("/api/models/status").json()["models"]["whisper"]
    assert st["state"] == "absent"
    # A second cancel has nothing to stop.
    assert client.post("/api/models/whisper/cancel").status_code == 409


def test_cancel_when_not_downloading_409(client):
    assert client.post("/api/models/whisper/cancel").status_code == 409


def test_cancel_queued_dequeues_without_touching_partials(client, home, fake_spawn):
    """Cancel on a QUEUED model removes it from the queue; no worker ever ran,
    so partial files from an earlier attempt stay for hf resume."""
    write_partial(home, "qwen3_embed", 500)
    client.post("/api/models/whisper/download")
    client.post("/api/models/parakeet/download")
    client.post("/api/models/qwen3_embed/download")
    r = client.post("/api/models/qwen3_embed/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is True
    assert r.json()["dequeued"] is True
    assert "qwen3_embed" not in fake_spawn  # never started
    st = client.get("/api/models/status").json()["models"]["qwen3_embed"]
    assert st["state"] == "absent"
    assert st["queue_position"] is None
    assert st["bytes_on_disk"] >= 500  # partials untouched
    # The running downloads were not disturbed.
    assert fake_spawn["whisper"].terminated is False
    assert fake_spawn["parakeet"].terminated is False


def test_cancel_queued_leaves_the_rest_of_the_queue_in_order(client, fake_spawn):
    client.post("/api/models/whisper/download")
    client.post("/api/models/parakeet/download")
    client.post("/api/models/qwen3_embed/download")
    client.post("/api/models/gliner/download")
    assert client.post("/api/models/qwen3_embed/cancel").status_code == 200
    st = client.get("/api/models/status").json()["models"]
    assert st["gliner"]["state"] == "queued"
    assert st["gliner"]["queue_position"] == 1


def test_cancel_drops_early_sentinel_so_retry_works(client, home, fake_spawn):
    """Snapshot repos fetch small files first — for ECAPA/GLiNER that is the
    sentinel itself, so a cancelled half-download must not read 'installed'
    (which would also 409 the retry)."""
    client.post("/api/models/ecapa_speaker/download")
    sentinel = install_fake(home, "ecapa_speaker")  # hf wrote it early
    assert client.post("/api/models/ecapa_speaker/cancel").status_code == 200
    assert not os.path.exists(sentinel)
    st = client.get("/api/models/status").json()["models"]["ecapa_speaker"]
    assert st["state"] == "absent"
    assert client.post("/api/models/ecapa_speaker/download").status_code == 200


def test_failed_worker_drops_early_sentinel(client, home, fake_spawn):
    client.post("/api/models/gliner/download")
    sentinel = install_fake(home, "gliner")
    fake_spawn["gliner"].rc = 1
    st = client.get("/api/models/status").json()["models"]["gliner"]
    assert st["state"] == "error"
    assert not os.path.exists(sentinel)


# ── delete ───────────────────────────────────────────────────────────────


def test_delete_refused_while_downloading(client, home, fake_spawn):
    client.post("/api/models/whisper/download")
    r = client.delete("/api/models/whisper")
    assert r.status_code == 409
    assert "cancel" in r.json()["detail"].lower()


def test_delete_installed_model(client, home):
    sentinel = install_fake(home, "qwen3_embed")
    r = client.delete("/api/models/qwen3_embed")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert not os.path.exists(sentinel)
    assert not os.path.exists(os.path.dirname(sentinel))
    st = client.get("/api/models/status").json()["models"]["qwen3_embed"]
    assert st["state"] == "absent"


def test_delete_nothing_on_disk_409(client):
    assert client.delete("/api/models/whisper").status_code == 409


def test_delete_queued_model_dequeues_then_deletes(client, home, fake_spawn):
    """Delete on a QUEUED model: out of the queue first, then disk cleanup."""
    write_partial(home, "qwen3_embed", 700)
    client.post("/api/models/whisper/download")
    client.post("/api/models/parakeet/download")
    client.post("/api/models/qwen3_embed/download")
    r = client.delete("/api/models/qwen3_embed")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["dequeued"] is True
    st = client.get("/api/models/status").json()["models"]["qwen3_embed"]
    assert st["state"] == "absent"
    assert st["queue_position"] is None
    assert st["bytes_on_disk"] == 0


def test_delete_queued_model_with_nothing_on_disk_is_a_dequeue(client, fake_spawn):
    client.post("/api/models/whisper/download")
    client.post("/api/models/parakeet/download")
    client.post("/api/models/qwen3_embed/download")
    r = client.delete("/api/models/qwen3_embed")
    # Honoring the delete meant dequeuing; nothing on disk is not a conflict.
    assert r.status_code == 200
    assert r.json()["deleted"] is False
    assert r.json()["dequeued"] is True
    st = client.get("/api/models/status").json()["models"]["qwen3_embed"]
    assert st["state"] == "absent"


def test_delete_resident_gguf_stops_llama_server(client, home, monkeypatch):
    from server.local import llama_server

    install_fake(home, "local_gemma")
    stopped = []
    monkeypatch.setattr(llama_server, "resident_key", lambda: "local_gemma")
    monkeypatch.setattr(llama_server, "stop", lambda: stopped.append(True))
    r = client.delete("/api/models/local_gemma")
    assert r.status_code == 200
    assert r.json()["stopped_llama_server"] is True
    assert "stopped" in r.json()["message"].lower()
    assert stopped == [True]
    assert not (home / "models" / get_entry("local_gemma").dir_name).exists()


def test_delete_non_resident_gguf_leaves_llama_server(client, home, monkeypatch):
    from server.local import llama_server

    install_fake(home, "local_gemma")
    monkeypatch.setattr(llama_server, "resident_key", lambda: None)
    monkeypatch.setattr(
        llama_server, "stop", lambda: (_ for _ in ()).throw(AssertionError("must not stop"))
    )
    r = client.delete("/api/models/local_gemma")
    assert r.status_code == 200
    assert r.json()["stopped_llama_server"] is False


# ── status poll ──────────────────────────────────────────────────────────


def test_status_covers_catalog_and_tracks_progress(client, home, fake_spawn):
    catalog_keys = {m["key"] for m in client.get("/api/models/catalog").json()["models"]}
    client.post("/api/models/parakeet/download")
    # Simulate hf writing partial bytes into the target dir mid-download.
    part = home / "models" / get_entry("parakeet").dir_name / "model.safetensors.incomplete"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"y" * 1000)
    status = client.get("/api/models/status").json()["models"]
    assert set(status) == catalog_keys
    assert status["parakeet"]["state"] == "downloading"
    assert status["parakeet"]["bytes_on_disk"] >= 1000
    for key in catalog_keys:
        assert {"state", "bytes_on_disk", "size_bytes_estimate", "progress", "error"} <= set(
            status[key]
        )


# ── shared progress (the ONE number every surface shows) ────────────────


def write_partial(home, key: str, nbytes: int) -> None:
    """Simulate hf's partial download bytes in the entry's directory."""
    part = home / "models" / get_entry(key).dir_name / "blob.incomplete"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"x" * nbytes)


def test_progress_matches_bytes_over_estimate_everywhere(client, home, monkeypatch, fake_spawn):
    """/status and /catalog serve the identical manager-computed fraction."""
    monkeypatch.setattr(sizes, "estimate", lambda e: 1000)
    write_partial(home, "parakeet", 250)
    client.post("/api/models/parakeet/download")
    st = client.get("/api/models/status").json()["models"]["parakeet"]
    cat = {m["key"]: m for m in client.get("/api/models/catalog").json()["models"]}["parakeet"]
    assert st["progress"] == 0.25
    assert cat["progress"] == 0.25


def test_progress_caps_at_99_until_installed(home, monkeypatch):
    """bytes_on_disk includes hf bookkeeping, so it can overshoot the estimate
    before the download is done — the number must sit at 0.99, never 100,
    until the sentinel check says installed."""
    entry = get_entry("parakeet")
    monkeypatch.setattr(sizes, "estimate", lambda e: 1000)
    write_partial(home, "parakeet", 5000)
    assert manager.progress_of(entry) == 0.99
    install_fake(home, "parakeet")
    assert manager.progress_of(entry) == 1.0


def test_progress_none_without_estimate(home, monkeypatch):
    monkeypatch.setattr(sizes, "estimate", lambda e: None)
    assert manager.progress_of(get_entry("parakeet")) is None


def test_progress_never_goes_backwards_within_a_run(client, home, monkeypatch, fake_spawn):
    """Mid-run the real HF size can replace a smaller fallback estimate; the
    raw ratio drops but the displayed number must hold its high-water mark."""
    entry = get_entry("parakeet")
    est = {"v": 1000}
    monkeypatch.setattr(sizes, "estimate", lambda e: est["v"])
    write_partial(home, "parakeet", 500)
    client.post("/api/models/parakeet/download")
    assert manager.progress_of(entry) == 0.5
    est["v"] = 2000  # estimate revised upward mid-run: raw ratio halves
    assert manager.progress_of(entry) == 0.5
    write_partial(home, "parakeet", 1500)  # bytes catch up past the mark
    assert manager.progress_of(entry) == 0.75


def test_progress_highwater_resets_between_runs(client, home, monkeypatch, fake_spawn):
    entry = get_entry("parakeet")
    est = {"v": 1000}
    monkeypatch.setattr(sizes, "estimate", lambda e: est["v"])
    write_partial(home, "parakeet", 900)
    client.post("/api/models/parakeet/download")
    assert manager.progress_of(entry) == 0.9
    client.post("/api/models/parakeet/cancel")
    # A new run against a (now honest) bigger estimate starts from real bytes,
    # not from the previous run's mark.
    est["v"] = 9000
    client.post("/api/models/parakeet/download")
    assert manager.progress_of(entry) == 0.1


def test_progress_is_none_while_queued(client, home, monkeypatch, fake_spawn):
    """A queued model has no run yet, so no progress — even with partial bytes
    on disk from an earlier attempt."""
    monkeypatch.setattr(sizes, "estimate", lambda e: 1000)
    write_partial(home, "qwen3_embed", 400)
    client.post("/api/models/whisper/download")
    client.post("/api/models/parakeet/download")
    client.post("/api/models/qwen3_embed/download")
    assert manager.progress_of(get_entry("qwen3_embed")) is None
    st = client.get("/api/models/status").json()["models"]["qwen3_embed"]
    assert st["progress"] is None


def test_progress_highwater_resets_on_dequeue_start(client, home, monkeypatch, fake_spawn):
    """Dequeue-start is a run boundary exactly like a direct start: a stale
    high-water mark (here written by a status poll over the absent-with-
    partials row) must not leak into the dequeued run."""
    entry = get_entry("qwen3_embed")
    est = {"v": 1000}
    monkeypatch.setattr(sizes, "estimate", lambda e: est["v"])
    write_partial(home, "qwen3_embed", 900)
    # An idle status poll marks the absent row at 900/1000 = 0.9.
    assert manager.progress_of(entry) == 0.9
    # Fill both slots, queue the model, then free a slot so the pump starts it.
    client.post("/api/models/whisper/download")
    client.post("/api/models/parakeet/download")
    client.post("/api/models/qwen3_embed/download")
    est["v"] = 9000  # honest revised estimate lands before the run starts
    fake_spawn["whisper"].rc = 0
    st = client.get("/api/models/status").json()["models"]["qwen3_embed"]
    assert st["state"] == "downloading"
    # 900/9000 — derived from real bytes, not the stale 0.9 mark.
    assert manager.progress_of(entry) == 0.1
