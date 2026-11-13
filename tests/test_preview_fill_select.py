"""preview_fill on a dropdown chooses the option.

A field turn called preview_fill on a <select> and got "Fill failed: Element
is not an <input>, <textarea> or [contenteditable] element", then spent two
more calls on a script that set the value by hand. The fill now chooses the
option of a select and types into every other element.
"""

import asyncio

from server.preview import manager


class _Locator:
    def __init__(self, tag: str, log: list):
        self.tag, self.log = tag, log

    async def evaluate(self, expression: str):
        return self.tag

    async def select_option(self, value: str):
        self.log.append(("select", value))

    async def fill(self, value: str):
        self.log.append(("fill", value))


class _Session:
    def __init__(self, tag: str):
        self.log: list = []
        outer = self

        class _Page:
            def locator(self, selector: str):
                return _Locator(tag, outer.log)

        class _Browser:
            page = _Page()

            async def ensure_started(self):
                return None

        self.browser = _Browser()


def _fill(monkeypatch, tag: str, value: str):
    session = _Session(tag)

    async def own(payload, verb):
        return session

    monkeypatch.setattr(manager, "_require_own_session", own)
    payload = {"session_name": "demo", "selector": "#count", "value": value}
    return asyncio.run(manager.fill_in_preview(payload)), session.log


def test_a_select_gets_its_option_chosen(monkeypatch):
    (ok, message), log = _fill(monkeypatch, "SELECT", "10")
    assert ok and message == "Selected '10' in '#count'"
    assert log == [("select", "10")]


def test_an_input_is_filled_as_before(monkeypatch):
    (ok, message), log = _fill(monkeypatch, "INPUT", "hello")
    assert ok and message == "Filled '#count'"
    assert log == [("fill", "hello")]
