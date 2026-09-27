"""Writers of the user config layer never lose each other's changes.

A Discover install finalises on a worker thread and read-modify-writes
config.user.json (write_entry), while PUT /api/config and a feature-flag toggle
do the same on the event loop. A write that lands between another writer's read
and its save must not be overwritten by that save. The interleaving is forced:
the install's read blocks until the other writer had its chance to run.
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure import config as cfg
from server.model_browser import install


@pytest.fixture
def user_file(tmp_path, monkeypatch):
    path = tmp_path / "config.user.json"
    path.write_text(json.dumps({"model_mode": "local", "feature_flags": {"auto_memory": True}}))
    monkeypatch.setattr(cfg, "_active_user_config_path", lambda: str(path))
    cfg._invalidate_cache()
    yield path
    cfg._invalidate_cache()


def _put_mode() -> None:
    app = FastAPI()
    app.include_router(cfg.router)
    assert TestClient(app).put("/api/config", json={"model_mode": "hybrid"}).status_code == 200


def _toggle_flag() -> None:
    cfg.set_feature_flag("auto_memory", False)


@pytest.mark.parametrize(
    ("other_writer", "survives"),
    [
        (_put_mode, lambda saved: saved["model_mode"] == "hybrid"),
        (_toggle_flag, lambda saved: saved["feature_flags"]["auto_memory"] is False),
    ],
    ids=["put-api-config", "feature-flag"],
)
def test_a_write_during_an_install_keeps_both_changes(
    user_file, monkeypatch, other_writer, survives
):
    read_done, resume = threading.Event(), threading.Event()
    real_load = cfg._load_user_config
    key, entry = install.build_recommended_entry("local_gemma")
    worker = threading.Thread(target=install.write_entry, args=(key, entry))

    def _install_read_then_pause():
        data = real_load()
        if threading.current_thread() is worker:
            read_done.set()
            resume.wait(5)
        return data

    # Patched where every writer resolves it (install reads config_mod's attribute).
    monkeypatch.setattr(cfg, "_load_user_config", _install_read_then_pause)
    worker.start()
    assert read_done.wait(5), "the install never read the user layer"

    other = threading.Thread(target=other_writer)
    other.start()
    # Without the lock the other writer finishes inside the install's
    # read-modify-write; with it, it waits for the install to save.
    other.join(0.5)
    resume.set()
    worker.join(5)
    other.join(5)
    assert not worker.is_alive() and not other.is_alive()

    saved = json.loads(user_file.read_text())
    assert key in saved.get("chat_models", {}), "the install's entry was lost"
    assert survives(saved), "the concurrent write was lost"
