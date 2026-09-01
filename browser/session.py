from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image
from playwright.async_api import async_playwright, BrowserContext, Page

from browser import dom_processor, pacing
import config

logger = logging.getLogger(__name__)

_REALISTIC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_COOKIE_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "[aria-label*='accept' i]",
    "button[id*='accept']",
    "button[class*='accept' i]",
]

_DEBUG_DIR = Path(__file__).resolve().parent.parent / "debug"


class BrowserSession:
    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._debug_turn: int = 0  # instance-level so multiple sessions don't collide

    def _on_new_page(self, page: Page) -> None:
        # Popups/new tabs are not driven by the agent — close them to prevent
        # a malicious site hijacking the active page pointer via window.open.
        if self._page is not None and page is not self._page:
            logger.warning("Blocked popup/new tab: %s", page.url or "<about:blank>")
            asyncio.create_task(self._safe_close_page(page))
            return
        self._page = page

    async def _safe_close_page(self, page: Page) -> None:
        try:
            await page.close()
        except Exception:
            pass

    def _on_download(self, download) -> None:
        suggested = getattr(download, "suggested_filename", "<unknown>")
        logger.warning("Blocked download: %s", suggested)
        asyncio.create_task(self._safe_cancel_download(download))

    async def _safe_cancel_download(self, download) -> None:
        try:
            await download.cancel()
        except Exception:
            pass

    def _register_context_handlers(self) -> None:
        self._context.on("page", self._on_new_page)
        self._context.on("download", self._on_download)

    def _require_page(self) -> dict | None:
        if self._page is None:
            return {"error": "No active page — call POST /reset to recover"}
        return None

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=config.HEADLESS)
        self._context = await self._browser.new_context(
            user_agent=_REALISTIC_UA,
            viewport={"width": 1280, "height": 800},
            accept_downloads=False,
        )

        self._register_context_handlers()
        self._page = await self._context.new_page()

    async def reset(self, url: str | None) -> dict:
        try:
            # Clear the page pointer before building the new context. The "page"
            # handler fires while new_page() below is still awaiting, and it
            # treats any page that is not self._page as a popup to be closed —
            # so a stale pointer here would make reset close the very page it
            # just created, leaving the session unrecoverable.
            self._page = None

            if self._context is not None:
                try:
                    await self._context.close()
                except Exception:
                    pass

            self._context = await self._browser.new_context(
                user_agent=_REALISTIC_UA,
                viewport={"width": 1280, "height": 800},
                accept_downloads=False,
            )

            self._register_context_handlers()
            self._page = await self._context.new_page()

            if url and url.startswith("http"):
                try:
                    await self._page.goto(url, wait_until="domcontentloaded")
                    await self._settle()
                    await self._try_dismiss_cookie_banner()
                except Exception:
                    pass

            return {"reset": True, "url": self._page.url}
        except Exception:
            logger.exception("Reset failed")
            return {"error": "Reset failed"}

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def _settle(self) -> None:
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        try:
            await self._page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            pass

    async def _try_dismiss_cookie_banner(self) -> None:
        for sel in _COOKIE_SELECTORS:
            try:
                locator = self._page.locator(sel).first
                if await locator.is_visible(timeout=200):
                    await locator.click()
                    await self._settle()  # wait for any post-accept navigation
                    return
            except Exception:
                pass

    async def get_state(self) -> dict:
        guard = self._require_page()
        if guard is not None:
            return guard
        try:
            try:
                await self._page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass

            # Default True: assume loading until the evaluate confirms otherwise
            loading = True
            try:
                loading = await self._page.evaluate("() => document.readyState !== 'complete'")
            except Exception:
                pass

            mark_data = await dom_processor.inject_marks(self._page)
            marks = mark_data.get("marks", [])
            total_page_marks = mark_data.get("total", len(marks))

            raw_png = await self._page.screenshot(type="png")
            img = Image.open(io.BytesIO(raw_png))
            img.thumbnail((1024, 1024), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75)
            screenshot_b64 = base64.b64encode(buf.getvalue()).decode()

            if config.DEBUG:
                debug_dir = _DEBUG_DIR / f"turn_{self._debug_turn:02d}"
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / "screenshot.jpg").write_bytes(buf.getvalue())
                (debug_dir / "marks.json").write_text(json.dumps(marks, indent=2))
                self._debug_turn += 1

            return {
                "url": self._page.url,
                "dom_marks": marks,
                "total_page_marks": total_page_marks,
                "screenshot_b64": screenshot_b64,
                "loading": loading,
            }
        except Exception:
            logger.exception("get_state failed")
            return {"error": "Could not capture page state"}

    async def navigate(self, url: str) -> dict:
        guard = self._require_page()
        if guard is not None:
            return guard
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return {"error": f"Unsupported URL scheme '{parsed.scheme}' — only http and https are allowed"}
            await pacing.pre_action_delay()
            await self._page.goto(url, wait_until="domcontentloaded")
            await self._settle()
            await self._try_dismiss_cookie_banner()
            return await self.get_state()
        except Exception:
            logger.exception("Navigation failed")
            return {"error": "Navigation failed"}

    async def click(self, element_id: int) -> dict:
        guard = self._require_page()
        if guard is not None:
            return guard
        try:
            await pacing.pre_action_delay()
            locator = self._page.locator(f"[data-mark-id='{element_id}']").first
            if await locator.count() == 0:
                return {"error": f"element {element_id} no longer exists — call get_state to refresh"}
            await locator.click()
            await self._settle()
            return await self.get_state()
        except Exception:
            logger.exception("Click failed")
            return {"error": "Click failed"}

    async def type_text(self, element_id: int, text: str) -> dict:
        guard = self._require_page()
        if guard is not None:
            return guard
        try:
            await pacing.pre_action_delay()
            locator = self._page.locator(f"[data-mark-id='{element_id}']").first
            if await locator.count() == 0:
                return {"error": f"element {element_id} no longer exists — call get_state to refresh"}
            await locator.click()
            await locator.press_sequentially(text, delay=pacing.typing_delay_ms())
            await self._settle()
            return await self.get_state()
        except Exception:
            logger.exception("Type failed")
            return {"error": "Type failed"}

    async def scroll(self, direction: str) -> dict:
        guard = self._require_page()
        if guard is not None:
            return guard
        try:
            await pacing.pre_action_delay()
            await pacing.eased_scroll(self._page, direction)
            await self._settle()
            return await self.get_state()
        except Exception:
            logger.exception("Scroll failed")
            return {"error": "Scroll failed"}

    async def press_key(self, key: str) -> dict:
        guard = self._require_page()
        if guard is not None:
            return guard
        try:
            await pacing.pre_action_delay()
            await self._page.keyboard.press(key)
            await self._settle()
            return await self.get_state()
        except Exception:
            logger.exception("Key press failed")
            return {"error": "Key press failed"}

    async def wait(self, seconds: float) -> dict:
        guard = self._require_page()
        if guard is not None:
            return guard
        try:
            await asyncio.sleep(seconds)
            return await self.get_state()
        except Exception:
            logger.exception("Wait failed")
            return {"error": "Wait failed"}
