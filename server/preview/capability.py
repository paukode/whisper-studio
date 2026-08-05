"""Runtime capability probe: is Playwright's Chromium actually usable?

Two independent, cheap checks (no subprocess spawn on the hot path):
  1. the `playwright` package is importable
  2. its Chromium browser binary is present on disk

Both must pass. Checked live on every call so a corrupted/deleted install
makes the preview tools disappear from the catalog again automatically,
without waiting for a restart or a flag re-toggle.
"""

from __future__ import annotations

import functools
import glob
import importlib.util
import json
import logging
import os
import platform

log = logging.getLogger("whisper-studio")


def is_packaged_install() -> bool:
    """True when running from a packaged .app bundle.

    The macOS shell always sets ``WHISPER_HOME`` before spawning the backend
    (the same signal :func:`server.infrastructure.paths.app_home` keys on); a
    dev checkout never does. In a packaged install the code tree is signed and
    read-only, so runtime pip installs / browser downloads are impossible —
    the Playwright browser ships inside the bundle instead."""
    return bool(os.environ.get("WHISPER_HOME", "").strip())


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


@functools.lru_cache(maxsize=1)
def _required_browser_dirs() -> tuple[str, ...]:
    """Directory names (under the browsers root) of the Chromium builds the
    installed playwright version launches, read from playwright's own registry
    (``playwright/driver/package/browsers.json``).

    ``chromium.launch(headless=True)`` — the only launch in server/preview —
    executes the *chromium-headless-shell* build on playwright 1.6x (verified:
    removing that directory breaks the launch), and ``playwright install
    chromium`` always installs the full chromium build alongside it. Require
    both, at the exact pinned revision, so a stale cache left over from an
    older playwright does not report ready and then fail at launch time.

    Returns an empty tuple when the registry cannot be read (unexpected
    playwright layout) — the caller falls back to a glob heuristic."""
    spec = importlib.util.find_spec("playwright")
    if spec is None or not spec.origin:
        return ()
    registry_path = os.path.join(os.path.dirname(spec.origin), "driver", "package", "browsers.json")
    try:
        with open(registry_path, encoding="utf-8") as f:
            registry = json.load(f)
        wanted = {"chromium", "chromium-headless-shell"}
        dirs = tuple(
            f"{b['name'].replace('-', '_')}-{b['revision']}"
            for b in registry.get("browsers", [])
            if b.get("name") in wanted and b.get("revision")
        )
        return dirs if len(dirs) == len(wanted) else ()
    except Exception as e:  # noqa: BLE001 — registry format is not ours to trust
        log.warning("Could not read playwright browser registry %s: %s", registry_path, e)
        return ()


def is_chromium_installed() -> bool:
    """True iff the Chromium builds this playwright version needs exist,
    fully installed, under Playwright's browsers directory.

    Checks for the `INSTALLATION_COMPLETE` marker file Playwright itself
    writes into each versioned browser directory on a successful install,
    rather than guessing an executable path — the app-bundle name/location
    varies across Playwright versions and channels (e.g. newer downloads are
    branded "Google Chrome for Testing.app", not "Chromium.app"), but the
    marker file's presence and meaning is Playwright's own stable contract.
    The set of required directories (full chromium + the headless shell that
    ``headless=True`` actually executes) comes from playwright's own registry
    at the exact pinned revisions; if that registry cannot be read, fall back
    to accepting any chromium build's marker."""
    if not is_playwright_importable():
        return False
    root = _browsers_root()
    if not os.path.isdir(root):
        return False
    required = _required_browser_dirs()
    if required:
        return all(os.path.isfile(os.path.join(root, d, "INSTALLATION_COMPLETE")) for d in required)
    markers = glob.glob(os.path.join(root, "chromium*", "INSTALLATION_COMPLETE"))
    return len(markers) > 0


def preview_capability_ok() -> bool:
    """Both checks must pass for preview_* tools to be offered at all."""
    return is_playwright_importable() and is_chromium_installed()
