"""Shared fixtures and Playwright stand-ins.

The suite never launches a browser and never touches the network. Every
Playwright object below is a fake that records what was asked of it, so the
session logic can be driven through paths that would otherwise need a live
page — popups, downloads, resets and failures included.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image


def make_png(size: tuple[int, int] = (24, 16)) -> bytes:
    """A real, tiny PNG — get_state runs it through Pillow for real."""
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


# ── Fake Playwright ───────────────────────────────────────────────────────────

class FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class FakeMouse:
    def __init__(self) -> None:
        self.deltas: list[float] = []

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        self.deltas.append(delta_y)


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> "FakeLocator":
        return self

    async def count(self) -> int:
        return self._page.locator_counts.get(self._selector, 0)

    async def is_visible(self, timeout: int | None = None) -> bool:
        return self._selector in self._page.visible_selectors

    async def click(self) -> None:
        if self._page.click_error is not None:
            raise self._page.click_error
        self._page.clicked.append(self._selector)

    async def press_sequentially(self, text: str, delay: int | None = None) -> None:
        self._page.typed.append((self._selector, text, delay))


class FakePage:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self.closed = False
        self.keyboard = FakeKeyboard()
        self.mouse = FakeMouse()

        self.goto_calls: list[str] = []
        self.clicked: list[str] = []
        self.typed: list[tuple[str, str, int | None]] = []

        # Configurable page state
        self.locator_counts: dict[str, int] = {}
        self.visible_selectors: set[str] = set()
        self.marks_payload: dict = {
            "marks": [{"id": 1, "tag": "button", "text": "OK", "type": None}],
            "total": 3,
        }
        self.ready_state_complete = True
        self.screenshot_bytes = make_png()

        # Configurable failures
        self.evaluate_error: Exception | None = None
        self.screenshot_error: Exception | None = None
        self.goto_error: Exception | None = None
        self.click_error: Exception | None = None

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        return None

    async def evaluate(self, script: str):
        if self.evaluate_error is not None:
            raise self.evaluate_error
        if "readyState" in script:
            return not self.ready_state_complete
        return self.marks_payload

    async def screenshot(self, type: str = "png") -> bytes:
        if self.screenshot_error is not None:
            raise self.screenshot_error
        return self.screenshot_bytes

    async def goto(self, url: str, wait_until: str | None = None) -> None:
        if self.goto_error is not None:
            raise self.goto_error
        self.goto_calls.append(url)
        self.url = url

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, browser: "FakeBrowser", **kwargs) -> None:
        self.browser = browser
        self.kwargs = kwargs
        self.closed = False
        self.handlers: dict[str, list] = {}
        self.pages: list[FakePage] = []
        self.new_page_error: Exception | None = None

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, payload) -> None:
        for handler in list(self.handlers.get(event, [])):
            handler(payload)

    async def new_page(self) -> FakePage:
        if self.new_page_error is not None:
            raise self.new_page_error
        page = FakePage()
        self.pages.append(page)
        # Real Playwright fires the context "page" event as the page appears,
        # i.e. before new_page() returns. Reproducing that ordering is the
        # whole point of this fake — it is what the reset regression needs.
        self.emit("page", page)
        return page

    async def close(self) -> None:
        self.closed = True
        for page in self.pages:
            page.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []
        self.closed = False
        self.new_context_error: Exception | None = None

    async def new_context(self, **kwargs) -> FakeContext:
        if self.new_context_error is not None:
            raise self.new_context_error
        context = FakeContext(self, **kwargs)
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser
        self.launch_kwargs: dict | None = None

    async def launch(self, headless: bool = True) -> FakeBrowser:
        self.launch_kwargs = {"headless": headless}
        return self._browser


class FakePlaywright:
    def __init__(self) -> None:
        self.browser = FakeBrowser()
        self.chromium = FakeChromium(self.browser)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakePlaywrightFactory:
    """Stands in for async_playwright(), whose .start() yields the driver."""

    def __init__(self, playwright: FakePlaywright) -> None:
        self._playwright = playwright

    async def start(self) -> FakePlaywright:
        return self._playwright


class FakeDownload:
    def __init__(self, suggested_filename: str = "payload.exe") -> None:
        self.suggested_filename = suggested_filename
        self.cancelled = False
        self.cancel_error: Exception | None = None

    async def cancel(self) -> None:
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled = True


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_playwright(monkeypatch) -> FakePlaywright:
    from browser import session as session_module

    playwright = FakePlaywright()
    monkeypatch.setattr(
        session_module, "async_playwright", lambda: FakePlaywrightFactory(playwright)
    )
    return playwright


class _RecordingClock:
    """Swallows pacing sleeps so the suite runs instantly, but records them."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)


@pytest.fixture
def no_pacing(monkeypatch) -> _RecordingClock:
    from browser import pacing

    clock = _RecordingClock()
    # pacing looks up asyncio.sleep through its module global, so replacing the
    # global with a shim keeps the patch local to this module.
    monkeypatch.setattr(pacing, "asyncio", clock)
    return clock


@pytest.fixture
async def started_session(fake_playwright, no_pacing):
    from browser.session import BrowserSession

    session = BrowserSession()
    await session.start()
    return session
