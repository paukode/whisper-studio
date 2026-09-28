"""A preview's browser starts at most once and never outlives its session.

ensure_started checked ``self.page`` and then awaited several Playwright calls
before assigning it, so concurrent first users (the Live pane's screencast, a
parallel preview_screenshot, another chat inspecting the same preview) each
launched a Chromium; close() only closed the last one assigned. A stop during
a cold start orphaned the browser being built, and a failure after the launch
leaked the driver and Chromium it had already started.
"""

import asyncio

import pytest

from server.preview import browser as browser_mod
from server.preview.browser import BrowserSession


class _Fake:
    """Stands in for the Playwright driver and records what is still open."""

    def __init__(self, *, launch_gate: asyncio.Event | None = None, fail_page: bool = False):
        self.launch_gate = launch_gate
        self.fail_page = fail_page
        self.launches = 0
        self.open: set[str] = set()

    # async_playwright() -> object with .start()
    def __call__(self):
        return self

    async def start(self):
        self.open.add("driver")
        return self

    async def stop(self):
        self.open.discard("driver")

    @property
    def chromium(self):
        return self

    async def launch(self, headless=True):
        self.launches += 1
        if self.launch_gate is not None:
            await self.launch_gate.wait()
        n = self.launches
        self.open.add(f"browser{n}")
        fake = self

        class Browser:
            async def new_context(self, viewport=None):
                class Context:
                    def set_default_timeout(self, ms):
                        pass

                    def set_default_navigation_timeout(self, ms):
                        pass

                    async def route(self, pattern, handler):
                        pass

                    async def new_page(self):
                        if fake.fail_page:
                            raise RuntimeError("new_page failed")

                        class Page:
                            def on(self, event, cb):
                                pass

                        return Page()

                    async def close(self):
                        pass

                return Context()

            async def close(self):
                fake.open.discard(f"browser{n}")

        return Browser()


@pytest.fixture
def fake_playwright(monkeypatch):
    def install(**kw) -> _Fake:
        fake = _Fake(**kw)
        monkeypatch.setattr(browser_mod, "_async_playwright", fake)
        return fake

    return install


def test_concurrent_first_users_share_one_launch(fake_playwright):
    async def scenario():
        gate = asyncio.Event()
        fake = fake_playwright(launch_gate=gate)
        session = BrowserSession()
        starts = [asyncio.create_task(session.ensure_started()) for _ in range(3)]
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.gather(*starts)
        assert fake.launches == 1, "each concurrent caller launched its own Chromium"
        await session.close()
        assert fake.open == set(), f"left running after close: {fake.open}"

    asyncio.run(scenario())


def test_close_during_a_cold_start_leaves_nothing_running(fake_playwright):
    async def scenario():
        gate = asyncio.Event()
        fake = fake_playwright(launch_gate=gate)
        session = BrowserSession()
        start = asyncio.create_task(session.ensure_started())
        await asyncio.sleep(0.01)
        await session.close()  # preview_stop lands mid-launch
        gate.set()
        with pytest.raises(RuntimeError, match="stopped"):
            await start
        assert fake.open == set(), f"left running after stop: {fake.open}"
        with pytest.raises(RuntimeError, match="stopped"):
            await session.ensure_started()
        assert fake.launches == 1, "a stopped session relaunched its browser"

    asyncio.run(scenario())


def test_a_failure_after_the_launch_closes_what_it_started(fake_playwright):
    async def scenario():
        fake = fake_playwright(fail_page=True)
        session = BrowserSession()
        with pytest.raises(RuntimeError, match="new_page failed"):
            await session.ensure_started()
        assert fake.open == set(), f"a failed start leaked: {fake.open}"
        assert session.page is None and session.browser is None

    asyncio.run(scenario())
