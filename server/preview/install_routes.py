"""Install flow for the live-preview feature's Playwright dependency.

Mirrors server/index/routes.py's background-thread + polling-status pattern
rather than inventing a streaming mechanism — there's only ever one
Playwright/Chromium install for the whole app, unlike per-workspace index
builds.

This is a deliberate, explicit Settings-panel action, not routed through the
chat approval system: the user navigates to Settings and clicks a labeled
button — there's no LLM in the loop deciding to install anything, the same
contract every other feature flag in this app already has.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading

from fastapi import APIRouter

from server.preview.capability import is_chromium_installed, is_playwright_importable

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/preview", tags=["preview"])

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
    return {
        "playwright_importable": is_playwright_importable(),
        "chromium_installed": is_chromium_installed(),
        "flag_enabled": is_enabled("preview_tools"),
        "installing": install["installing"],
        "stage": install["stage"],
        "error": install["error"],
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
