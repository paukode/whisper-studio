"""Playwright browser/context/page wrapper for one preview session.

Each session gets its own ephemeral BrowserContext (never a persistent
profile) and its own Page, created lazily on first navigation. Console and
network activity are captured into bounded ring buffers so
preview_console_logs/preview_network can read them back as plain text.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

log = logging.getLogger("whisper-studio")

_CONSOLE_CAP = 500  # entries, not bytes — short structured records
_NETWORK_CAP = 500

_ALLOWED_SCHEMES = {"http", "https"}

# Timeouts so a cold Chromium start or a not-yet-ready / unreachable dev server
# fails fast with a clear error instead of hanging the chat turn indefinitely.
_LAUNCH_TIMEOUT_S = 60  # chromium.launch cold start (first use is slow)
_ACTION_TIMEOUT_MS = 15_000  # click/fill/screenshot/inspect default
_NAV_TIMEOUT_MS = 30_000  # page.goto default


class BrowserSession:
    """One Playwright Browser + BrowserContext + Page, plus bounded ring
    buffers for console messages and network events."""

    def __init__(self):
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.console_log: list[dict] = []
        self.network_log: list[dict] = []

    async def ensure_started(self):
        if self.page is not None:
            return
        import asyncio

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        try:
            self.browser = await asyncio.wait_for(
                self._playwright.chromium.launch(headless=True),
                timeout=_LAUNCH_TIMEOUT_S,
            )
        except asyncio.TimeoutError as e:
            await self._playwright.stop()
            self._playwright = None
            raise RuntimeError(
                f"Chromium did not launch within {_LAUNCH_TIMEOUT_S}s — the "
                "Playwright browser install may be incomplete."
            ) from e
        # new_context() (not launch_persistent_context()) — ephemeral, no
        # cookies/profile persisted across sessions or shared with the
        # user's real browser.
        self.context = await self.browser.new_context(viewport={"width": 1280, "height": 800})
        # Bound every subsequent action/navigation so a not-yet-ready dev
        # server or an unreachable URL fails fast instead of wedging the turn.
        self.context.set_default_timeout(_ACTION_TIMEOUT_MS)
        self.context.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
        # Registered at the context level, before any page exists, so it
        # also covers popups/new tabs — blocks file://, data:, chrome:// etc,
        # the concrete filesystem/privilege escapes. Approval on preview_navigate
        # is the deliberateness gate; this is the last-line technical backstop.
        await self.context.route("**/*", self._guard_navigation)
        self.page = await self.context.new_page()
        self.page.on("console", self._on_console)
        # Uncaught JS exceptions fire "pageerror", NOT "console" — without this
        # they'd be invisible to preview_console_logs (e.g. a handler that throws
        # a TypeError on a missing element). Record them as error-level entries.
        self.page.on("pageerror", self._on_page_error)
        self.page.on("requestfinished", self._on_request_finished)
        self.page.on("requestfailed", self._on_request_failed)

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
        import asyncio

        asyncio.create_task(self._record_request(request, failed=False))

    def _on_request_failed(self, request):
        import asyncio

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
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:  # noqa: BLE001
            log.warning("Error closing preview browser session: %s", e)
