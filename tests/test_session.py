"""BrowserSession driven against fake Playwright objects.

No browser is launched. The point of these is the session's own defensive
behaviour: refusing popups and downloads, guarding against a missing page, and
rebuilding itself correctly on reset.
"""

from __future__ import annotations

import asyncio

import pytest

import config
from browser.session import BrowserSession
from tests.conftest import FakeDownload, FakePage


async def _drain() -> None:
    """Let handler-spawned tasks (create_task) run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)


# ── Startup ───────────────────────────────────────────────────────────────────

async def test_start_builds_a_hardened_context(fake_playwright, no_pacing) -> None:
    session = BrowserSession()
    await session.start()

    assert fake_playwright.chromium.launch_kwargs == {"headless": config.HEADLESS}
    context = fake_playwright.browser.contexts[0]
    assert context.kwargs["accept_downloads"] is False
    assert context.kwargs["viewport"] == {"width": 1280, "height": 800}
    assert "Chrome/" in context.kwargs["user_agent"]
    assert session._page is context.pages[0]


async def test_start_registers_popup_and_download_handlers(started_session) -> None:
    context = started_session._context
    assert "page" in context.handlers
    assert "download" in context.handlers


async def test_stop_closes_the_browser_and_driver(fake_playwright, no_pacing) -> None:
    session = BrowserSession()
    await session.start()
    await session.stop()
    assert fake_playwright.browser.closed is True
    assert fake_playwright.stopped is True


async def test_stop_is_safe_before_start() -> None:
    await BrowserSession().stop()  # no attributes set yet; must not raise


# ── Popups and downloads are refused ──────────────────────────────────────────

async def test_popup_is_closed_and_does_not_steal_the_page_pointer(started_session) -> None:
    original = started_session._page
    popup = FakePage(url="https://evil.example/popup")

    started_session._context.emit("page", popup)
    await _drain()

    assert popup.closed is True
    assert started_session._page is original
    assert original.closed is False


async def test_popup_close_failure_is_swallowed(started_session) -> None:
    popup = FakePage()

    async def boom() -> None:
        raise RuntimeError("already gone")

    popup.close = boom
    started_session._context.emit("page", popup)
    await _drain()  # must not raise


async def test_download_is_cancelled(started_session) -> None:
    download = FakeDownload("invoice.pdf")
    started_session._context.emit("download", download)
    await _drain()
    assert download.cancelled is True


async def test_download_cancel_failure_is_swallowed(started_session) -> None:
    download = FakeDownload()
    download.cancel_error = RuntimeError("cannot cancel")
    started_session._context.emit("download", download)
    await _drain()  # must not raise


# ── The missing-page guard ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get_state(),
        lambda s: s.navigate("https://example.com/"),
        lambda s: s.click(1),
        lambda s: s.type_text(1, "x"),
        lambda s: s.scroll("down"),
        lambda s: s.press_key("Enter"),
        lambda s: s.wait(0),
    ],
)
async def test_every_action_guards_against_a_missing_page(started_session, call) -> None:
    started_session._page = None
    result = await call(started_session)
    assert result == {"error": "No active page — call POST /reset to recover"}


# ── reset ─────────────────────────────────────────────────────────────────────

async def test_reset_does_not_close_the_page_it_just_created(started_session) -> None:
    """Regression: the popup handler used to eat reset's own new page.

    reset() left _page pointing at the old, closed page while the new context
    was built, so the "page" event fired by new_page() failed the identity
    check and the fresh page was scheduled for closure — leaving /reset
    reporting success on a dead session.
    """
    old_page = started_session._page

    result = await started_session.reset(None)
    await _drain()

    assert result["reset"] is True
    new_page = started_session._page
    assert new_page is not old_page
    assert new_page.closed is False, "reset closed the page it had just created"
    assert old_page.closed is True


async def test_reset_builds_a_fresh_context_with_the_same_hardening(started_session) -> None:
    old_context = started_session._context
    await started_session.reset(None)
    await _drain()

    assert started_session._context is not old_context
    assert old_context.closed is True
    assert started_session._context.kwargs["accept_downloads"] is False
    assert "page" in started_session._context.handlers


async def test_reset_navigates_when_given_a_url(started_session) -> None:
    result = await started_session.reset("https://example.com/start")
    await _drain()

    assert started_session._page.goto_calls == ["https://example.com/start"]
    assert result == {"reset": True, "url": "https://example.com/start"}


async def test_reset_without_a_url_leaves_the_page_blank(started_session) -> None:
    result = await started_session.reset(None)
    await _drain()
    assert started_session._page.goto_calls == []
    assert result["url"] == "about:blank"


async def test_reset_ignores_a_non_http_url(started_session) -> None:
    """Defence in depth — the model already rejects these."""
    await started_session.reset("file:///etc/passwd")
    await _drain()
    assert started_session._page.goto_calls == []


async def test_reset_survives_a_failed_navigation(started_session) -> None:
    """A dead target must still leave a usable session behind."""
    # reset() builds a brand-new context, so the goto failure has to be
    # injected at the browser level to reach the page it will create.
    browser = started_session._browser
    original_new_context = browser.new_context

    async def new_context_with_bad_goto(**kwargs):
        context = await original_new_context(**kwargs)
        inner = context.new_page

        async def patched():
            page = await inner()
            page.goto_error = RuntimeError("net::ERR_NAME_NOT_RESOLVED")
            return page

        context.new_page = patched
        return context

    browser.new_context = new_context_with_bad_goto

    result = await started_session.reset("https://unreachable.example/")
    await _drain()

    assert result["reset"] is True
    assert started_session._page is not None
    assert started_session._page.closed is False


async def test_reset_reports_an_error_when_the_context_cannot_be_built(started_session) -> None:
    started_session._browser.new_context_error = RuntimeError("browser is gone")
    result = await started_session.reset(None)
    assert result == {"error": "Reset failed"}


async def test_reset_error_message_does_not_leak_internals(started_session) -> None:
    started_session._browser.new_context_error = RuntimeError(
        "connect ECONNREFUSED /tmp/secret-socket-path"
    )
    result = await started_session.reset(None)
    assert "secret-socket-path" not in result["error"]


# ── get_state ─────────────────────────────────────────────────────────────────

async def test_get_state_returns_the_documented_shape(started_session) -> None:
    started_session._page.url = "https://example.com/"
    state = await started_session.get_state()

    assert set(state) == {
        "url", "dom_marks", "total_page_marks", "screenshot_b64", "loading"
    }
    assert state["url"] == "https://example.com/"
    assert state["dom_marks"] == [{"id": 1, "tag": "button", "text": "OK", "type": None}]
    assert state["total_page_marks"] == 3
    assert state["loading"] is False
    assert isinstance(state["screenshot_b64"], str) and state["screenshot_b64"]


async def test_get_state_screenshot_is_base64_jpeg(started_session) -> None:
    import base64

    state = await started_session.get_state()
    raw = base64.b64decode(state["screenshot_b64"])
    assert raw.startswith(b"\xff\xd8\xff"), "JPEG magic bytes"


async def test_get_state_reports_loading_when_the_document_is_not_complete(
    started_session,
) -> None:
    started_session._page.ready_state_complete = False
    state = await started_session.get_state()
    assert state["loading"] is True


async def test_get_state_assumes_loading_when_the_probe_fails(started_session) -> None:
    """Fail safe: an unanswerable readyState means 'assume still loading'."""
    page = started_session._page
    real_evaluate = page.evaluate

    async def evaluate(script):
        if "readyState" in script:
            raise RuntimeError("execution context destroyed")
        return await real_evaluate(script)

    page.evaluate = evaluate
    state = await started_session.get_state()
    assert state["loading"] is True


async def test_get_state_returns_an_error_when_the_screenshot_fails(started_session) -> None:
    started_session._page.screenshot_error = RuntimeError("target closed")
    assert await started_session.get_state() == {"error": "Could not capture page state"}


async def test_get_state_error_does_not_leak_internals(started_session) -> None:
    started_session._page.screenshot_error = RuntimeError("C:/Users/secret/path leaked")
    result = await started_session.get_state()
    assert "secret" not in result["error"]


async def test_debug_mode_writes_artifacts(started_session, monkeypatch, tmp_path) -> None:
    from browser import session as session_module

    monkeypatch.setattr(session_module.config, "DEBUG", True)
    monkeypatch.setattr(session_module, "_DEBUG_DIR", tmp_path / "debug")

    await started_session.get_state()
    await started_session.get_state()

    assert (tmp_path / "debug" / "turn_00" / "screenshot.jpg").exists()
    assert (tmp_path / "debug" / "turn_00" / "marks.json").exists()
    assert (tmp_path / "debug" / "turn_01" / "screenshot.jpg").exists()


async def test_debug_turn_counter_is_per_instance(fake_playwright, no_pacing) -> None:
    """Two sessions must not overwrite each other's debug output."""
    a, b = BrowserSession(), BrowserSession()
    assert a._debug_turn == 0 and b._debug_turn == 0
    a._debug_turn = 7
    assert b._debug_turn == 0


# ── navigate ──────────────────────────────────────────────────────────────────

async def test_navigate_loads_and_returns_state(started_session) -> None:
    state = await started_session.navigate("https://example.com/page")
    assert started_session._page.goto_calls == ["https://example.com/page"]
    assert state["url"] == "https://example.com/page"


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/", "javascript:alert(1)", "about:blank"]
)
async def test_navigate_refuses_non_http_schemes(started_session, url: str) -> None:
    """Second line of defence behind the Pydantic validator."""
    result = await started_session.navigate(url)
    assert "error" in result
    assert "only http and https" in result["error"]
    assert started_session._page.goto_calls == []


async def test_navigate_reports_failure_without_leaking(started_session) -> None:
    started_session._page.goto_error = RuntimeError("net::ERR at C:/secret/profile")
    result = await started_session.navigate("https://example.com/")
    assert result == {"error": "Navigation failed"}


async def test_navigate_paces_before_acting(started_session, no_pacing) -> None:
    await started_session.navigate("https://example.com/")
    assert no_pacing.sleeps, "a pre-action delay should have been awaited"


async def test_navigate_dismisses_a_cookie_banner_when_present(started_session) -> None:
    page = started_session._page
    page.visible_selectors.add("#onetrust-accept-btn-handler")
    await started_session.navigate("https://example.com/")
    assert "#onetrust-accept-btn-handler" in page.clicked


async def test_navigate_without_a_banner_clicks_nothing(started_session) -> None:
    await started_session.navigate("https://example.com/")
    assert started_session._page.clicked == []


# ── click / type / scroll / press_key / wait ──────────────────────────────────

async def test_click_uses_the_mark_id_selector(started_session) -> None:
    started_session._page.locator_counts["[data-mark-id='14']"] = 1
    await started_session.click(14)
    assert started_session._page.clicked == ["[data-mark-id='14']"]


async def test_click_on_a_stale_element_id_explains_itself(started_session) -> None:
    result = await started_session.click(99)
    assert result == {
        "error": "element 99 no longer exists — call get_state to refresh"
    }


async def test_click_failure_is_reported(started_session) -> None:
    page = started_session._page
    page.locator_counts["[data-mark-id='3']"] = 1
    page.click_error = RuntimeError("element is not clickable")
    assert await started_session.click(3) == {"error": "Click failed"}


async def test_type_focuses_then_types_with_a_delay(started_session) -> None:
    page = started_session._page
    page.locator_counts["[data-mark-id='7']"] = 1

    await started_session.type_text(7, "search query")

    assert page.clicked == ["[data-mark-id='7']"]
    selector, text, delay = page.typed[0]
    assert (selector, text) == ("[data-mark-id='7']", "search query")
    assert 50 <= delay <= 150


async def test_type_on_a_stale_element_id_explains_itself(started_session) -> None:
    result = await started_session.type_text(42, "x")
    assert "element 42 no longer exists" in result["error"]


@pytest.mark.parametrize("direction,sign", [("down", 1), ("up", -1)])
async def test_scroll_moves_a_full_viewport_step(started_session, direction, sign) -> None:
    await started_session.scroll(direction)
    deltas = started_session._page.mouse.deltas
    assert deltas
    assert round(sum(deltas)) == sign * 300


async def test_press_key_forwards_to_the_keyboard(started_session) -> None:
    await started_session.press_key("Escape")
    assert started_session._page.keyboard.pressed == ["Escape"]


async def test_press_key_failure_is_reported(started_session) -> None:
    async def boom(key):
        raise RuntimeError("Unknown key")

    started_session._page.keyboard.press = boom
    assert await started_session.press_key("Nonsense") == {"error": "Key press failed"}


async def test_wait_sleeps_then_returns_state(started_session, monkeypatch) -> None:
    from browser import session as session_module

    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(session_module.asyncio, "sleep", fake_sleep)
    state = await started_session.wait(2.5)

    assert 2.5 in slept
    assert "screenshot_b64" in state


# ── Error paths that must be swallowed rather than propagated ─────────────────

async def test_reset_survives_a_context_that_will_not_close(started_session) -> None:
    async def refuse():
        raise RuntimeError("context already detached")

    started_session._context.close = refuse

    result = await started_session.reset(None)
    await _drain()

    assert result["reset"] is True
    assert started_session._page.closed is False


async def test_settle_swallows_load_state_timeouts(started_session) -> None:
    """A page that never reaches networkidle must not fail the action."""

    async def always_timeout(state, timeout=None):
        raise TimeoutError(f"timed out waiting for {state}")

    started_session._page.wait_for_load_state = always_timeout

    state = await started_session.navigate("https://example.com/")
    assert "screenshot_b64" in state


async def test_cookie_banner_probe_errors_are_swallowed(started_session) -> None:
    """A selector that throws must not stop the remaining candidates."""
    page = started_session._page
    real_locator = page.locator

    def locator(selector):
        if selector == "#onetrust-accept-btn-handler":
            raise RuntimeError("invalid selector in this frame")
        return real_locator(selector)

    page.locator = locator
    page.visible_selectors.add("button[id*='accept']")

    await started_session.navigate("https://example.com/")
    assert "button[id*='accept']" in page.clicked


async def test_type_failure_is_reported(started_session) -> None:
    page = started_session._page
    page.locator_counts["[data-mark-id='5']"] = 1

    async def refuse(text, delay=None):
        raise RuntimeError("element is not editable")

    original = page.locator

    def locator(selector):
        loc = original(selector)
        loc.press_sequentially = refuse
        return loc

    page.locator = locator
    assert await started_session.type_text(5, "hello") == {"error": "Type failed"}


async def test_scroll_failure_is_reported(started_session) -> None:
    async def refuse(delta_x, delta_y):
        raise RuntimeError("target closed")

    started_session._page.mouse.wheel = refuse
    assert await started_session.scroll("down") == {"error": "Scroll failed"}


async def test_wait_failure_is_reported(started_session, monkeypatch) -> None:
    from browser import session as session_module

    async def refuse(seconds):
        raise RuntimeError("event loop is closed")

    monkeypatch.setattr(session_module.asyncio, "sleep", refuse)
    assert await started_session.wait(1.0) == {"error": "Wait failed"}
