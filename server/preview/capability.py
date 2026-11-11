"""Runtime capability probe: is Playwright's Chromium actually usable?

Two independent, cheap checks (no subprocess spawn on the hot path):
  1. the `playwright` package is importable
  2. its Chromium browser binary is present on disk

Both must pass. setup.sh installs both and the app bundle ships both, so a
failed check means a broken install: each preview tool call checks live and
refuses with :func:`missing_browser_message`, which names the fix.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import logging
import os

log = logging.getLogger("whisper-studio")


def is_packaged_install() -> bool:
    """True when running from a packaged .app bundle.

    The macOS shell always sets ``WHISPER_HOME`` before spawning the backend
    (the same signal :func:`server.infrastructure.paths.app_home` keys on); a
    dev checkout never does. In a packaged install the Playwright browser ships
    inside the signed, read-only bundle, so a missing browser means the app
    must be installed again."""
    return bool(os.environ.get("WHISPER_HOME", "").strip())


def _browsers_root() -> str:
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser("~/Library/Caches/ms-playwright")


def is_playwright_importable() -> bool:
    return importlib.util.find_spec("playwright") is not None


@functools.lru_cache(maxsize=1)
def _required_browser_dirs() -> tuple[str, ...]:
    """Directory names (under the browsers root) of the Chromium builds the
    installed playwright version launches, read from playwright's own registry
    (``playwright/driver/package/browsers.json``).

    ``chromium.launch(headless=True)``, the only launch in server/preview,
    executes the *chromium-headless-shell* build on playwright 1.6x (verified:
    removing that directory breaks the launch), and ``playwright install
    chromium`` always installs the full chromium build alongside it. Require
    both, at the exact pinned revision, so a stale cache left over from an
    older playwright does not report ready and then fail at launch time.

    Returns an empty tuple when the registry cannot be read (unexpected
    playwright layout, logged with the path): then Chromium does not count as
    installed, because no build can be checked against the pinned revisions."""
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
    except Exception as e:  # noqa: BLE001 (registry format is not ours to trust)
        log.warning("Could not read playwright browser registry %s: %s", registry_path, e)
        return ()


def is_chromium_installed() -> bool:
    """True iff the Chromium builds this playwright version needs exist,
    fully installed, under Playwright's browsers directory.

    Checks for the `INSTALLATION_COMPLETE` marker file Playwright itself
    writes into each versioned browser directory on a successful install,
    rather than guessing an executable path, the app-bundle name/location
    varies across Playwright versions and channels (e.g. newer downloads are
    branded "Google Chrome for Testing.app", not "Chromium.app"), but the
    marker file's presence and meaning is Playwright's own stable contract.
    The set of required directories (full chromium + the headless shell that
    ``headless=True`` actually executes) comes from playwright's own registry
    at the exact pinned revisions; if that registry cannot be read, no build
    counts as installed (an older revision would report ready and then fail
    at launch)."""
    if not is_playwright_importable():
        return False
    root = _browsers_root()
    if not os.path.isdir(root):
        return False
    required = _required_browser_dirs()
    if not required:
        return False
    return all(os.path.isfile(os.path.join(root, d, "INSTALLATION_COMPLETE")) for d in required)


def preview_capability_ok() -> bool:
    """Both checks must pass for preview_* tools to be offered at all."""
    return is_playwright_importable() and is_chromium_installed()


def missing_browser_message() -> str | None:
    """None when live preview can run, else the refusal a preview tool
    returns: what is missing and how to repair the install."""
    if preview_capability_ok():
        return None
    if is_packaged_install():
        return (
            "Live preview cannot run: the browser is missing from the app bundle. "
            "Install Whisper Studio again."
        )
    return (
        "Live preview cannot run: Playwright or its Chromium browser is missing. "
        "Run setup.sh again to install them."
    )
