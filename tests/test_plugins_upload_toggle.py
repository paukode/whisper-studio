"""Add-a-plugin-from-the-UI + live enable/disable (no restart).

Upload validates statically (safe name, parses, defines register) and lands the
plugin DISABLED. Enabling calls load_plugin so its executors register into the
live EXECUTORS registry and its tool schemas flow into the per-turn tool pool
(assemble_full_catalog); disabling pops both immediately. Protected plugins stay
locked.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.plugins as plugins
from server.executors import EXECUTOR_META, EXECUTORS

GOOD_PLUGIN = b"""
__version__ = "2.3.4"
__description__ = "E2E ping plugin"


def register(app, executor_registry):
    def e2e_ping(tool_input, transcript, attachments):
        return "pong"

    executor_registry["e2e_ping"] = e2e_ping
"""

NO_REGISTER = b"__version__ = '1.0'\nx = 1\n"
SYNTAX_ERR = b"def register(app, r):\n    return  (\n"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    monkeypatch.setattr(plugins, "PLUGINS_DIR", str(pdir))
    monkeypatch.setattr(plugins, "PLUGINS_CONFIG_PATH", str(tmp_path / "plugins_config.json"))
    # Fresh live-load bookkeeping per test.
    monkeypatch.setattr(plugins, "_loaded_plugins", {})
    monkeypatch.setattr(plugins, "_plugin_executors", {})
    monkeypatch.setattr(plugins, "_plugin_tools", {})
    monkeypatch.setattr(plugins, "_plugin_side_effects", {})
    # Snapshot the shared executor registry so a leaked plugin executor never
    # bleeds into another test.
    exec_before = dict(EXECUTORS)
    meta_before = dict(EXECUTOR_META)

    app = FastAPI()
    app.include_router(plugins.router)
    monkeypatch.setattr(plugins, "_app", app)
    yield TestClient(app), pdir

    EXECUTORS.clear()
    EXECUTORS.update(exec_before)
    EXECUTOR_META.clear()
    EXECUTOR_META.update(meta_before)


def _upload(client, name: str, data: bytes, overwrite: str = "false"):
    return client.post(
        "/api/plugins/upload",
        files={"file": (name, data, "text/x-python")},
        data={"overwrite": overwrite},
    )


# --- upload validation ---------------------------------------------------


def test_upload_good_file_accepted_and_disabled(client):
    c, pdir = client
    r = _upload(c, "e2e_plug.py", GOOD_PLUGIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "e2e_plug" and body["enabled"] is False
    assert (pdir / "e2e_plug.py").is_file()
    # Appears in the list as disabled, not yet loaded (no tools).
    listed = {p["name"]: p for p in c.get("/api/plugins").json()["plugins"]}
    assert listed["e2e_plug"]["enabled"] is False
    assert listed["e2e_plug"]["tools"] == []


def test_upload_missing_register_rejected(client):
    c, _ = client
    r = _upload(c, "noreg.py", NO_REGISTER)
    assert r.status_code == 400 and "register" in r.json()["detail"]


def test_upload_syntax_error_rejected(client):
    c, _ = client
    r = _upload(c, "broken.py", SYNTAX_ERR)
    assert r.status_code == 400 and "syntax" in r.json()["detail"].lower()


@pytest.mark.parametrize("bad", ["foo/bar.py", "..evil.py", "__init__.py", "plug.txt", "README.py"])
def test_upload_unsafe_name_rejected(client, bad):
    c, _ = client
    r = _upload(c, bad, GOOD_PLUGIN)
    assert r.status_code == 400, f"{bad!r} should be rejected"


def test_upload_oversize_rejected(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(plugins, "_MAX_PLUGIN_BYTES", 32)
    r = _upload(c, "big.py", GOOD_PLUGIN)
    assert r.status_code == 400 and "limit" in r.json()["detail"].lower()


def test_upload_duplicate_409_then_overwrite(client):
    c, _ = client
    assert _upload(c, "dup.py", GOOD_PLUGIN).status_code == 200
    assert _upload(c, "dup.py", GOOD_PLUGIN).status_code == 409
    assert _upload(c, "dup.py", GOOD_PLUGIN, overwrite="true").status_code == 200


# --- live enable / disable ----------------------------------------------


def test_enable_registers_executor_and_pool_then_disable_hides(client):
    c, _ = client
    _upload(c, "e2e_plug.py", GOOD_PLUGIN)
    assert "e2e_ping" not in EXECUTORS

    # Enable → load_plugin runs, executor + tool schema go live.
    r = c.patch("/api/plugins/e2e_plug/toggle")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and body["loaded"] is True
    assert body["restartRequired"] is False  # tool executor only, no routes
    assert "e2e_ping" in EXECUTORS
    assert "e2e_ping" in {t["name"] for t in plugins.plugin_tools()}

    # Real, not cosmetic: the tool is in the per-turn pool.
    from server.chat.tool_pool import assemble_full_catalog

    pool_names = {t["name"] for t in assemble_full_catalog()}
    assert "e2e_ping" in pool_names

    # The list endpoint reflects the live registration.
    listed = {p["name"]: p for p in c.get("/api/plugins").json()["plugins"]}
    assert listed["e2e_plug"]["enabled"] is True
    assert "e2e_ping" in listed["e2e_plug"]["tools"]

    # Disable → executor + tool disappear immediately.
    r2 = c.patch("/api/plugins/e2e_plug/toggle")
    assert r2.status_code == 200 and r2.json()["enabled"] is False
    assert "e2e_ping" not in EXECUTORS
    assert "e2e_ping" not in {t["name"] for t in plugins.plugin_tools()}
    assert "e2e_ping" not in {t["name"] for t in assemble_full_catalog()}


def test_toggle_unknown_plugin_404(client):
    c, _ = client
    assert c.patch("/api/plugins/does_not_exist/toggle").status_code == 404


def test_protected_plugin_cannot_be_toggled(client):
    c, pdir = client
    # Even present on disk, the protected plugin refuses to toggle off.
    (pdir / "security_checks.py").write_bytes(GOOD_PLUGIN)
    r = c.patch("/api/plugins/security_checks/toggle")
    assert r.status_code == 409 and "safety" in r.json()["detail"].lower()
