"""Live-preview install/status flow, packaged-bundle vs dev-checkout.

A packaged .app (WHISPER_HOME set, the same signal paths.app_home keys on)
ships playwright + Chromium inside the signed bundle and must NEVER attempt a
runtime pip/browser download: ready means "bundled browser resolves", missing
means "reinstall the app". A dev checkout keeps the original background-thread
install path.
"""

import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.preview import capability, install_routes
from server.preview.install_routes import _BUNDLE_MISSING_MSG, router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_install_state():
    """Each test starts from a pristine module-level install job."""
    with install_routes._lock:
        install_routes._INSTALL.update(installing=False, stage=None, error=None, log_tail=[])
    yield
    with install_routes._lock:
        install_routes._INSTALL.update(installing=False, stage=None, error=None, log_tail=[])


@pytest.fixture(autouse=True)
def _no_flag_io(monkeypatch):
    """Keep flag reads/writes away from the real config.json."""
    from server.infrastructure import config as config_mod
    from server.infrastructure import feature_flags

    flags: dict[str, bool] = {}
    monkeypatch.setattr(
        config_mod, "set_feature_flag", lambda name, enabled: flags.__setitem__(name, enabled)
    )
    monkeypatch.setattr(feature_flags, "is_enabled", lambda name: flags.get(name, False))
    return flags


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _make_bundled_browsers(root):
    """Create the exact versioned dirs + INSTALLATION_COMPLETE markers the
    pinned playwright expects, the way `playwright install chromium` (and
    therefore the bundle) lays them out."""
    required = capability._required_browser_dirs()
    assert required, "playwright registry should be readable in the venv"
    assert len(required) == 2, "expected full chromium + headless shell"
    for d in required:
        (root / d).mkdir(parents=True)
        (root / d / "INSTALLATION_COMPLETE").write_text("")
    return required


# ---------------------------------------------------------------------------
# Capability probe
# ---------------------------------------------------------------------------


def test_registry_names_both_chromium_builds():
    """headless=True launches the headless shell; the probe must require it
    alongside full chromium, at exact revisions from playwright's registry."""
    required = capability._required_browser_dirs()
    names = sorted(d.rsplit("-", 1)[0] for d in required)
    assert names == ["chromium", "chromium_headless_shell"]
    assert all(d.rsplit("-", 1)[1].isdigit() for d in required)


def test_chromium_installed_with_exact_revisions(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    _make_bundled_browsers(tmp_path)
    assert capability.is_chromium_installed() is True


def test_stale_revision_does_not_count(tmp_path, monkeypatch):
    """A cache left by an older playwright must not report ready — the pinned
    playwright would fail at launch time with 'Executable doesn't exist'."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    stale = tmp_path / "chromium-1"
    stale.mkdir(parents=True)
    (stale / "INSTALLATION_COMPLETE").write_text("")
    assert capability.is_chromium_installed() is False


def test_missing_headless_shell_not_ready(tmp_path, monkeypatch):
    """Full chromium alone is not enough: headless launches execute the
    chromium-headless-shell build on this playwright version."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    required = _make_bundled_browsers(tmp_path)
    shell = next(d for d in required if "headless_shell" in d)
    (tmp_path / shell / "INSTALLATION_COMPLETE").unlink()
    assert capability.is_chromium_installed() is False


def test_glob_fallback_when_registry_unreadable(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(capability, "_required_browser_dirs", lambda: ())
    marker_dir = tmp_path / "chromium-9999"
    marker_dir.mkdir(parents=True)
    (marker_dir / "INSTALLATION_COMPLETE").write_text("")
    assert capability.is_chromium_installed() is True


def test_is_packaged_install_follows_whisper_home(monkeypatch):
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    assert capability.is_packaged_install() is False
    monkeypatch.setenv("WHISPER_HOME", "/tmp/somewhere")
    assert capability.is_packaged_install() is True


# ---------------------------------------------------------------------------
# Packaged bundle: ready path (zero downloads)
# ---------------------------------------------------------------------------


def test_status_ready_when_bundled(tmp_path, monkeypatch, client):
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "pw"))
    _make_bundled_browsers(tmp_path / "pw")

    status = client.get("/api/preview/status").json()
    assert status["packaged"] is True
    assert status["playwright_importable"] is True
    assert status["chromium_installed"] is True
    assert status["error"] is None
    assert status["installing"] is False


def test_enable_in_bundle_flips_flag_without_installing(tmp_path, monkeypatch, client, _no_flag_io):
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "pw"))
    _make_bundled_browsers(tmp_path / "pw")

    resp = client.post("/api/preview/install").json()
    assert resp == {"started": False, "reason": "already installed", "enabled": True}
    assert _no_flag_io == {"preview_tools": True}
    assert client.get("/api/preview/status").json()["flag_enabled"] is True


# ---------------------------------------------------------------------------
# Packaged bundle: browser missing (broken install, never download)
# ---------------------------------------------------------------------------


def test_status_surfaces_bundle_missing_error(tmp_path, monkeypatch, client):
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "empty"))

    status = client.get("/api/preview/status").json()
    assert status["packaged"] is True
    assert status["chromium_installed"] is False
    assert status["error"] == _BUNDLE_MISSING_MSG


def test_install_refused_in_bundle_when_missing(tmp_path, monkeypatch, client):
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "empty"))

    ran = threading.Event()
    monkeypatch.setattr(install_routes, "_run_install", lambda: ran.set())

    resp = client.post("/api/preview/install").json()
    assert resp["started"] is False
    assert resp["error"] == _BUNDLE_MISSING_MSG
    assert not ran.wait(timeout=0.2), "packaged install must never start the download thread"
    status = client.get("/api/preview/status").json()
    assert status["installing"] is False
    assert status["error"] == _BUNDLE_MISSING_MSG


# ---------------------------------------------------------------------------
# Dev checkout: original install path preserved
# ---------------------------------------------------------------------------


def test_dev_install_path_preserved(tmp_path, monkeypatch, client):
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "empty"))

    ran = threading.Event()

    def fake_install():
        with install_routes._lock:
            install_routes._INSTALL.update(installing=False, stage="done")
        ran.set()

    monkeypatch.setattr(install_routes, "_run_install", fake_install)

    resp = client.post("/api/preview/install").json()
    assert resp == {"started": True}
    assert ran.wait(timeout=5), "dev checkout must run the background install"


def test_dev_status_has_no_bundle_error(tmp_path, monkeypatch, client):
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "empty"))

    status = client.get("/api/preview/status").json()
    assert status["packaged"] is False
    assert status["chromium_installed"] is False
    assert status["error"] is None
