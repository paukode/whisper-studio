"""Update check: is the running app older than the newest GitHub release?

``GET /api/update-check`` returns the running version, how it was installed
(the Mac app or a ``setup.sh`` source checkout), and the newest published
release of paukode/whisper-studio. The UI shows an Update notice only when
``update_available`` is true.

Where the running version comes from:

- The Mac app: ``CFBundleShortVersionString`` in the bundle's Info.plist.
  ``macapp/build_app.sh`` writes it from ``git describe`` at build time, and
  the backend tree lives at ``Contents/Resources/backend``, so the plist is
  two folders up from the repo root.
- A source checkout: ``git describe --tags --always`` on the code tree.

The GitHub request carries no user data: a fixed User-Agent and Accept
header, no version, no token. It has a short timeout, and any failure (no
network, rate limit, a malformed body) means "no update known". A good answer
is cached for some hours and a failure for a shorter time, so the check costs
at most a few requests a day. This module never changes permissions or
behaviour; it only reports.
"""

from __future__ import annotations

import asyncio
import logging
import os
import plistlib
import re
import subprocess
import time
from typing import Any

from fastapi import APIRouter

from server.infrastructure.paths import repo_root

log = logging.getLogger("whisper-studio")

RELEASES_URL = "https://api.github.com/repos/paukode/whisper-studio/releases/latest"
# The release page the Update button opens must be on this repo's releases,
# never a URL the response could point anywhere else.
RELEASE_PAGE_PREFIX = "https://github.com/paukode/whisper-studio/releases/"
FETCH_TIMEOUT_S = 5.0
CACHE_OK_S = 6 * 3600
CACHE_FAIL_S = 30 * 60
UPDATE_COMMANDS = ["git pull", "bash setup.sh"]

# "2.15.2", "v2.11.2.1", "2.15.2-453-g69dcdaf4", "2.6.0-sonic".
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(.*)$")
_DESCRIBE_AHEAD_RE = re.compile(r"^-(\d+)-g[0-9a-f]+")

router = APIRouter(prefix="/api/update-check", tags=["update-check"])

_cache: dict[str, Any] = {"at": 0.0, "ttl": 0.0, "release": None}
_lock = asyncio.Lock()
_running_version: str | None = None


def is_packaged() -> bool:
    """True inside the Mac app: the shell always sets WHISPER_HOME."""
    return bool(os.environ.get("WHISPER_HOME", "").strip())


def parse_version(text: str | None) -> tuple[tuple[int, ...], int] | None:
    """``(numbers, commits_ahead)`` for a tag or ``git describe`` string.

    ``commits_ahead`` is the ``N`` in ``X.Y.Z-N-gHASH`` (0 for a plain tag).
    Any other suffix (``-sonic``) is ignored. ``None`` when the text has no
    leading version number (a bare commit hash, an empty string)."""
    if not text:
        return None
    m = _VERSION_RE.match(text.strip())
    if not m:
        return None
    numbers = tuple(int(p) for p in m.group(1).split("."))
    ahead = _DESCRIBE_AHEAD_RE.match(m.group(2))
    return numbers, int(ahead.group(1)) if ahead else 0


def _padded(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)), b + (0,) * (width - len(b))


def is_older(current: str | None, latest: str | None) -> bool:
    """True only when ``current`` is a release before ``latest``.

    A build on the newest tag, or with commits beyond it
    (``2.15.2-453-g69dcdaf4`` against ``v2.15.2``), is not older. A build
    described from an earlier tag is older: ``git describe`` names the
    nearest tag the build contains, so the newer release is not in it. An
    unknown version on either side is never older."""
    cur = parse_version(current)
    new = parse_version(latest)
    if cur is None or new is None:
        return False
    cur_nums, new_nums = _padded(cur[0], new[0])
    return cur_nums < new_nums


def _bundle_plist_version() -> str | None:
    # repo_root() is Contents/Resources/backend inside the app.
    plist = os.path.join(repo_root(), os.pardir, os.pardir, "Info.plist")
    try:
        with open(plist, "rb") as fh:
            value = plistlib.load(fh).get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _git_describe_version() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", repo_root(), "describe", "--tags", "--always"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.strip() if out.returncode == 0 else ""
    return text[1:] if text.startswith("v") else (text or None)


def running_version() -> str | None:
    """The running app's version, read once per process."""
    global _running_version
    if _running_version is None:
        found = _bundle_plist_version() if is_packaged() else _git_describe_version()
        _running_version = found or ""
    return _running_version or None


async def fetch_latest_release() -> dict[str, str] | None:
    """The newest published release, or ``None`` on any failure."""
    import httpx

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Whisper-Studio"}
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, follow_redirects=False) as client:
            resp = await client.get(RELEASES_URL, headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 (any failure means no update known)
        log.debug("update check failed: %s", exc)
        return None
    return release_from_payload(data)


def release_from_payload(data: Any) -> dict[str, str] | None:
    """Keep the fields the UI needs, or ``None`` for a body that is not a
    published release of this repo."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name")
    url = data.get("html_url")
    if not isinstance(tag, str) or parse_version(tag) is None:
        return None
    if not isinstance(url, str) or not url.startswith(RELEASE_PAGE_PREFIX):
        return None
    published = data.get("published_at")
    return {
        "tag": tag,
        "version": tag[1:] if tag.startswith("v") else tag,
        "url": url,
        "published_at": published if isinstance(published, str) else "",
    }


async def latest_release() -> dict[str, str] | None:
    """The cached newest release; fetched again when the cache expires."""
    async with _lock:
        now = time.monotonic()
        if _cache["at"] and now - _cache["at"] < _cache["ttl"]:
            return _cache["release"]
        release = await fetch_latest_release()
        _cache.update(
            at=time.monotonic(),
            ttl=CACHE_OK_S if release else CACHE_FAIL_S,
            release=release,
        )
        return release


def reset_cache() -> None:
    """Forget the cached release and running version (tests)."""
    global _running_version
    _cache.update(at=0.0, ttl=0.0, release=None)
    _running_version = None


@router.get("")
async def update_check() -> dict[str, Any]:
    current = await asyncio.to_thread(running_version)
    release = await latest_release()
    install = "app" if is_packaged() else "source"
    return {
        "current": current,
        "install": install,
        "latest": release,
        "update_available": bool(release) and is_older(current, release["version"]),
        "update_commands": UPDATE_COMMANDS if install == "source" else [],
    }
