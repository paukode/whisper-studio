"""Playwright browser/context/page wrapper for one preview session.

Each session gets its own ephemeral BrowserContext (never a persistent
profile) and its own Page, created lazily on first navigation. Console and
network activity are captured into bounded ring buffers so
preview_console_logs/preview_network can read them back as plain text.
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse

log = logging.getLogger("whisper-studio")

_CONSOLE_CAP = 500  # entries, not bytes: short structured records
_NETWORK_CAP = 500

_ALLOWED_SCHEMES = {"http", "https"}

# Timeouts so a cold Chromium start or a not-yet-ready / unreachable dev server
# fails fast with a clear error instead of hanging the chat turn indefinitely.
_LAUNCH_TIMEOUT_S = 60  # chromium.launch cold start (first use is slow)
_ACTION_TIMEOUT_MS = 15_000  # click/fill/screenshot/inspect default
_NAV_TIMEOUT_MS = 30_000  # page.goto default


class BrowserSession:
    """One Playwright Browser + BrowserContext + Page, plus bounded ring
    buffers for console messages and network events.

    Starts at most once: concurrent first users (the Live pane's screencast, a
    parallel preview_screenshot, a second chat inspecting the same preview)
    share one launch instead of each starting a Chromium of which only the
    last assigned was ever closed. A close() during a start in flight makes
    that start tear down what it built, and a closed session never relaunches.
    """

    def __init__(self):
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.console_log: list[dict] = []
        self.network_log: list[dict] = []
        self._start_lock = asyncio.Lock()
        self._closed = False

    async def ensure_started(self):
        if self.page is not None:
            return
        async with self._start_lock:
            if self.page is not None:
                return
            if self._closed:
                raise RuntimeError(_CLOSED_MESSAGE)
            playwright, browser, context, page = await self._launch()
            if self._closed:
                # Stopped while it was starting: nothing else holds these.
                await _close_quietly(context, browser, playwright)
                raise RuntimeError(_CLOSED_MESSAGE)
            self._playwright, self.browser, self.context, self.page = (
                playwright,
                browser,
                context,
                page,
            )

    async def _launch(self):
        """Build a playwright driver, browser, context and page. Any failure
        closes whatever was already built before it propagates, so a failed
        start never leaves a driver or Chromium running."""
        playwright = await _async_playwright().start()
        browser = context = None
        try:
            try:
                browser = await asyncio.wait_for(
                    playwright.chromium.launch(headless=True),
                    timeout=_LAUNCH_TIMEOUT_S,
                )
            except asyncio.TimeoutError as e:
                raise RuntimeError(
                    f"Chromium did not launch within {_LAUNCH_TIMEOUT_S}s; the "
                    "Playwright browser install may be incomplete."
                ) from e
            # new_context() (not launch_persistent_context()): ephemeral, no
            # cookies/profile persisted across sessions or shared with the
            # user's real browser.
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            # Bound every subsequent action/navigation so a not-yet-ready dev
            # server or an unreachable URL fails fast instead of wedging the turn.
            context.set_default_timeout(_ACTION_TIMEOUT_MS)
            context.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
            # Registered at the context level, before any page exists, so it
            # also covers popups/new tabs. Blocks file://, data:, chrome:// etc,
            # the concrete filesystem/privilege escapes. Approval on preview_navigate
            # is the deliberateness gate; this is the last-line technical backstop.
            await context.route("**/*", self._guard_navigation)
            page = await context.new_page()
        except BaseException:
            await _close_quietly(context, browser, playwright)
            raise
        page.on("console", self._on_console)
        # Uncaught JS exceptions fire "pageerror", NOT "console"; without this
        # they'd be invisible to preview_console_logs (e.g. a handler that throws
        # a TypeError on a missing element). Record them as error-level entries.
        page.on("pageerror", self._on_page_error)
        page.on("requestfinished", self._on_request_finished)
        page.on("requestfailed", self._on_request_failed)
        return playwright, browser, context, page

    async def _guard_navigation(self, route, request):
        scheme = urlparse(request.url).scheme
        if scheme not in _ALLOWED_SCHEMES:
            log.warning("Preview browser blocked navigation to disallowed scheme: %s", request.url)
            await route.abort()
            return
        await route.continue_()

    def _on_console(self, msg):
        self.console_log.append({"level": msg.type, "text": msg.text, "ts": time.time()})
        if len(self.console_log) > _CONSOLE_CAP:
            del self.console_log[: len(self.console_log) - _CONSOLE_CAP]

    def _on_page_error(self, error):
        # error is a playwright Error (or str); str() gives name + message.
        self.console_log.append({"level": "error", "text": f"Uncaught {error}", "ts": time.time()})
        if len(self.console_log) > _CONSOLE_CAP:
            del self.console_log[: len(self.console_log) - _CONSOLE_CAP]

    def _on_request_finished(self, request):
        asyncio.create_task(self._record_request(request, failed=False))

    def _on_request_failed(self, request):
        asyncio.create_task(self._record_request(request, failed=True))

    async def _record_request(self, request, *, failed: bool):
        entry = {
            "method": request.method,
            "url": request.url,
            "status": None,
            "failed": failed,
            "ts": time.time(),
        }
        if not failed:
            try:
                resp = await request.response()
                entry["status"] = resp.status if resp else None
            except Exception:  # noqa: BLE001
                pass
        self.network_log.append(entry)
        if len(self.network_log) > _NETWORK_CAP:
            del self.network_log[: len(self.network_log) - _NETWORK_CAP]

    def console_text(self, *, level: str | None = None, lines: int = 100) -> str:
        entries = self.console_log
        if level:
            entries = [e for e in entries if e["level"] == level]
        entries = entries[-lines:]
        if not entries:
            return "(no console output yet)"
        return "\n".join(f"[{e['level']}] {e['text']}" for e in entries)

    def network_text(self, *, only_failed: bool = False, lines: int = 100) -> str:
        entries = self.network_log
        if only_failed:
            entries = [e for e in entries if e["failed"] or (e["status"] and e["status"] >= 400)]
        entries = entries[-lines:]
        if not entries:
            return "(no network activity yet)"
        rows = []
        for e in entries:
            status = "FAILED" if e["failed"] else str(e["status"] or "?")
            rows.append(f"{status:>6}  {e['method']:<6} {e['url']}")
        return "\n".join(rows)

    async def close(self):
        """Close the browser and driver, and mark the session closed so a
        start still in flight tears down its own launch instead of keeping it."""
        self._closed = True
        context, browser, playwright = self.context, self.browser, self._playwright
        self.page = self.context = self.browser = self._playwright = None
        await _close_quietly(context, browser, playwright)


_CLOSED_MESSAGE = "This preview session was stopped; start a new one with preview_start."


def _async_playwright():
    from playwright.async_api import async_playwright

    return async_playwright()


async def _close_quietly(context, browser, playwright) -> None:
    """Close each part on its own, so one failing close never leaves the
    others (a headless Chromium, the driver process) running."""
    for what, closer in (
        ("context", context.close if context else None),
        ("browser", browser.close if browser else None),
        ("playwright", playwright.stop if playwright else None),
    ):
        if closer is None:
            continue
        try:
            await closer()
        except Exception as e:  # noqa: BLE001
            log.warning("Error closing the preview %s: %s", what, e)
