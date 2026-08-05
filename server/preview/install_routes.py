"""Install flow for the live-preview feature's Playwright dependency.

Mirrors server/index/routes.py's background-thread + polling-status pattern
rather than inventing a streaming mechanism — there's only ever one
Playwright/Chromium install for the whole app, unlike per-workspace index
builds.

This is a deliberate, explicit Settings-panel action, not routed through the
chat approval system: the user navigates to Settings and clicks a labeled
button — there's no LLM in the loop deciding to install anything, the same
contract every other feature flag in this app already has.

Packaged installs (the macOS .app) never run this install: the bundle ships
playwright and its Chromium builds in Resources/ (PLAYWRIGHT_BROWSERS_PATH is
set by the shell), the code tree is signed and read-only, and a pip install or
browser download into it would fail — or break the bundle seal if it didn't.
There, "Enable" just flips the feature flag when the bundled browser is
present, and a missing browser is reported as a broken install to fix by
reinstalling the app, not by downloading.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading

from fastapi import APIRouter

from server.preview.capability import (
    is_chromium_installed,
    is_packaged_install,
    is_playwright_importable,
)

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/preview", tags=["preview"])

# Shown when a packaged .app lacks its bundled browser (corrupted/tampered
# install) — downloading into the signed bundle is not an option.
_BUNDLE_MISSING_MSG = "Preview browser missing from the app bundle — reinstall the app."

# Single global install job — there's only ever one Playwright/Chromium
# install for the whole app.
_INSTALL: dict = {"installing": False, "stage": None, "error": None, "log_tail": []}
_lock = threading.Lock()

_MAX_LOG_LINES = 200


def _append_log(line: str) -> None:
    with _lock:
        _INSTALL["log_tail"].append(line)
        _INSTALL["log_tail"] = _INSTALL["log_tail"][-_MAX_LOG_LINES:]


def _run_step(args: list[str]) -> bool:
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout or []:
        _append_log(line.rstrip())
    proc.wait()
    return proc.returncode == 0


def _run_install() -> None:
    try:
        with _lock:
            _INSTALL.update(stage="pip install playwright", error=None)
        if not _run_step([sys.executable, "-m", "pip", "install", "playwright"]):
            with _lock:
                _INSTALL.update(
                    installing=False,
                    error="pip install playwright failed — check network/PyPI access",
                )
            return

        with _lock:
            _INSTALL.update(stage="playwright install chromium")
        if not _run_step([sys.executable, "-m", "playwright", "install", "chromium"]):
            with _lock:
                _INSTALL.update(
                    installing=False,
                    error="chromium download failed — check disk space and network connectivity",
                )
            return

        if not is_chromium_installed():
            with _lock:
                _INSTALL.update(
                    installing=False,
                    error="Install reported success but Chromium was not found afterward",
                )
            return

        from server.infrastructure.config import set_feature_flag

        set_feature_flag("preview_tools", True)
        with _lock:
            _INSTALL.update(installing=False, stage="done", error=None)
        log.info("Live preview: Playwright + Chromium install complete; preview_tools enabled")
    except Exception as e:  # noqa: BLE001 — surface to the UI, don't crash the thread
        log.exception("Live preview install failed")
        with _lock:
            _INSTALL.update(installing=False, error=str(e))


@router.get("/status")
async def preview_status():
    from server.infrastructure.feature_flags import is_enabled

    with _lock:
        install = dict(_INSTALL)
    importable = is_playwright_importable()
    chromium = is_chromium_installed()
    packaged = is_packaged_install()
    error = install["error"]
    if error is None and packaged and not (importable and chromium):
        # A bundle that lost its browser can never self-heal by downloading;
        # surface that in the settings panel without waiting for a click.
        error = _BUNDLE_MISSING_MSG
    return {
        "playwright_importable": importable,
        "chromium_installed": chromium,
        "flag_enabled": is_enabled("preview_tools"),
        "packaged": packaged,
        "installing": install["installing"],
        "stage": install["stage"],
        "error": error,
        "log_tail": install["log_tail"],
    }


@router.post("/install")
async def preview_install():
    with _lock:
        if _INSTALL["installing"]:
            return {"started": False, "reason": "already installing"}
        if is_playwright_importable() and is_chromium_installed():
            from server.infrastructure.config import set_feature_flag

            set_feature_flag("preview_tools", True)
            return {"started": False, "reason": "already installed", "enabled": True}
        if is_packaged_install():
            # Signed read-only bundle: pip installs into its site-packages and
            # browser downloads into Resources/pw-browsers would fail (and
            # would break the code-signing seal if they didn't). The browser
            # ships with the app; if it is missing the install is broken.
            _INSTALL.update(installing=False, stage=None, error=_BUNDLE_MISSING_MSG)
            return {"started": False, "reason": "packaged install", "error": _BUNDLE_MISSING_MSG}
        _INSTALL.update(installing=True, stage="queued", error=None, log_tail=[])
    threading.Thread(target=_run_install, daemon=True, name="preview-install").start()
    return {"started": True}


@router.post("/disable")
async def preview_disable():
    """Off-switch — flips the flag without uninstalling Chromium, so
    re-enabling later is instant (no re-download)."""
    from server.infrastructure.config import set_feature_flag

    set_feature_flag("preview_tools", False)
    return {"enabled": False}
