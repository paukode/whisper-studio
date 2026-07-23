"""Runtime capability probe: is Playwright's Chromium actually usable?

Two independent, cheap checks (no subprocess spawn on the hot path):
  1. the `playwright` package is importable
  2. its Chromium browser binary is present on disk

Both must pass. Checked live on every call so a corrupted/deleted install
makes the preview tools disappear from the catalog again automatically,
without waiting for a restart or a flag re-toggle.
"""

from __future__ import annotations

import glob
import importlib.util
import logging
import os
import platform

log = logging.getLogger("whisper-studio")


def _browsers_root() -> str:
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return os.path.expanduser(override)
    system = platform.system()
    if system == "Darwin":
        return os.path.expanduser("~/Library/Caches/ms-playwright")
    if system == "Windows":
        return os.path.expanduser("~/AppData/Local/ms-playwright")
    return os.path.expanduser("~/.cache/ms-playwright")


def is_playwright_importable() -> bool:
    return importlib.util.find_spec("playwright") is not None


def is_chromium_installed() -> bool:
    """True iff a fully-installed Chromium build exists under Playwright's
    browsers directory.

    Checks for the `INSTALLATION_COMPLETE` marker file Playwright itself
    writes into each versioned browser directory on a successful install,
    rather than guessing an executable path — the app-bundle name/location
    varies across Playwright versions and channels (e.g. newer downloads are
    branded "Google Chrome for Testing.app", not "Chromium.app"), but the
    marker file's presence and meaning is Playwright's own stable contract."""
    if not is_playwright_importable():
        return False
    root = _browsers_root()
    if not os.path.isdir(root):
        return False
    markers = glob.glob(os.path.join(root, "chromium-*", "INSTALLATION_COMPLETE"))
    return len(markers) > 0


def preview_capability_ok() -> bool:
    """Both checks must pass for preview_* tools to be offered at all."""
    return is_playwright_importable() and is_chromium_installed()
