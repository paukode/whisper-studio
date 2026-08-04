"""Model download manager (/api/models/*): catalog shape, installed detection
against a throwaway WHISPER_HOME, download job lifecycle (409 on double start,
concurrency cap, cancel), delete rules, and the status poll endpoint.

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


def test_download_then_409_on_double_start(client, fake_spawn):
    assert client.post("/api/models/whisper/download").status_code == 200
    r = client.post("/api/models/whisper/download")
    assert r.status_code == 409
    assert "downloading" in r.json()["detail"].lower()


def test_download_409_when_installed(client, home, fake_spawn):
    install_fake(home, "parakeet")
    r = client.post("/api/models/parakeet/download")
    assert r.status_code == 409
    assert "installed" in r.json()["detail"].lower()


def test_download_concurrency_cap(client, fake_spawn):
    assert client.post("/api/models/whisper/download").status_code == 200
    assert client.post("/api/models/parakeet/download").status_code == 200
    r = client.post("/api/models/qwen3_embed/download")
    assert r.status_code == 409
    # Finishing one frees a slot.
    fake_spawn["whisper"].rc = 0
    assert client.post("/api/models/qwen3_embed/download").status_code == 200


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
