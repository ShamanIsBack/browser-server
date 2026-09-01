"""End-to-end HTTP behaviour, with the browser session faked out.

Driven through httpx's ASGI transport rather than TestClient, because that
skips the lifespan — the real lifespan would launch Chromium. The session and
lock the lifespan normally installs are injected directly instead.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

import config
import main

GOOD_KEY = "0123456789abcdef0123456789abcdef"
AUTH = {"X-API-Key": GOOD_KEY}

STATE = {
    "url": "https://example.com/",
    "dom_marks": [{"id": 1, "tag": "button", "text": "OK", "type": None}],
    "total_page_marks": 3,
    "screenshot_b64": "AAAA",
    "loading": False,
}


class FakeSession:
    """Records calls and returns whatever the test told it to."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.result: dict = STATE
        self.gate: asyncio.Event | None = None
        self.concurrent = 0
        self.max_concurrent = 0

    async def _run(self, name: str, *args) -> dict:
        self.calls.append((name, args))
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.gate is not None:
                await self.gate.wait()
            else:
                await asyncio.sleep(0)
            return self.result
        finally:
            self.concurrent -= 1

    async def get_state(self):            return await self._run("get_state")
    async def navigate(self, url):        return await self._run("navigate", url)
    async def click(self, eid):           return await self._run("click", eid)
    async def type_text(self, eid, text): return await self._run("type_text", eid, text)
    async def scroll(self, direction):    return await self._run("scroll", direction)
    async def press_key(self, key):       return await self._run("press_key", key)
    async def wait(self, seconds):        return await self._run("wait", seconds)
    async def reset(self, url):           return await self._run("reset", url)


@pytest.fixture
def session(monkeypatch) -> FakeSession:
    fake = FakeSession()
    monkeypatch.setattr(main, "_session", fake)
    monkeypatch.setattr(main, "_lock", asyncio.Lock())
    monkeypatch.setattr(config, "API_KEY", GOOD_KEY)
    return fake


@pytest.fixture
async def client(session):
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://server") as ac:
        yield ac


# ── Health ────────────────────────────────────────────────────────────────────

async def test_health_is_open_and_touches_no_browser(client, session) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert session.calls == []


# ── Auth enforcement over real HTTP ───────────────────────────────────────────

@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/state", None),
        ("post", "/navigate", {"url": "https://example.com/"}),
        ("post", "/click", {"element_id": 1}),
        ("post", "/type", {"element_id": 1, "text": "hi"}),
        ("post", "/scroll", {"direction": "down"}),
        ("post", "/press_key", {"key": "Enter"}),
        ("post", "/wait", {"seconds": 1}),
        ("post", "/reset", {}),
    ],
)
async def test_endpoints_require_the_key(client, session, method, path, body) -> None:
    call = getattr(client, method)
    resp = await (call(path) if body is None else call(path, json=body))
    assert resp.status_code == 401
    assert session.calls == []


async def test_bad_key_never_reaches_the_browser(client, session) -> None:
    resp = await client.post(
        "/navigate", json={"url": "https://example.com/"}, headers={"X-API-Key": "nope"}
    )
    assert resp.status_code == 401
    assert session.calls == []


# ── Happy paths ───────────────────────────────────────────────────────────────

async def test_state(client, session) -> None:
    resp = await client.get("/state", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == STATE
    assert session.calls == [("get_state", ())]


async def test_navigate_passes_the_url_through(client, session) -> None:
    resp = await client.post(
        "/navigate", json={"url": "https://example.com/page"}, headers=AUTH
    )
    assert resp.status_code == 200
    assert session.calls == [("navigate", ("https://example.com/page",))]


async def test_click(client, session) -> None:
    resp = await client.post("/click", json={"element_id": 14}, headers=AUTH)
    assert resp.status_code == 200
    assert session.calls == [("click", (14,))]


async def test_type(client, session) -> None:
    resp = await client.post(
        "/type", json={"element_id": 7, "text": "search query"}, headers=AUTH
    )
    assert resp.status_code == 200
    assert session.calls == [("type_text", (7, "search query"))]


async def test_scroll(client, session) -> None:
    resp = await client.post("/scroll", json={"direction": "down"}, headers=AUTH)
    assert resp.status_code == 200
    assert session.calls == [("scroll", ("down",))]


async def test_press_key(client, session) -> None:
    resp = await client.post("/press_key", json={"key": "Escape"}, headers=AUTH)
    assert resp.status_code == 200
    assert session.calls == [("press_key", ("Escape",))]


async def test_wait(client, session) -> None:
    resp = await client.post("/wait", json={"seconds": 2.5}, headers=AUTH)
    assert resp.status_code == 200
    assert session.calls == [("wait", (2.5,))]


async def test_reset_with_and_without_a_url(client, session) -> None:
    session.result = {"reset": True, "url": "https://example.com/"}

    resp = await client.post("/reset", json={}, headers=AUTH)
    assert resp.status_code == 200
    assert session.calls[-1] == ("reset", (None,))

    resp = await client.post("/reset", json={"url": "https://example.com/"}, headers=AUTH)
    assert resp.status_code == 200
    assert session.calls[-1] == ("reset", ("https://example.com/",))


# ── Validation rejections happen before the browser is touched ────────────────

@pytest.mark.parametrize(
    "path,body",
    [
        ("/navigate", {"url": "http://127.0.0.1/"}),
        ("/navigate", {"url": "http://169.254.169.254/"}),
        ("/navigate", {"url": "file:///etc/passwd"}),
        ("/reset", {"url": "http://10.0.0.1/"}),
        ("/click", {"element_id": 0}),
        ("/click", {"element_id": 99_999}),
        ("/type", {"element_id": 1, "text": "x" * 10_001}),
        ("/scroll", {"direction": "sideways"}),
        ("/press_key", {"key": "Enter\n"}),
        ("/press_key", {"key": "rm -rf /"}),
        ("/wait", {"seconds": 99}),
        ("/wait", {"seconds": -1}),
    ],
)
async def test_invalid_bodies_are_422_and_never_reach_the_session(
    client, session, path, body
) -> None:
    resp = await client.post(path, json=body, headers=AUTH)
    assert resp.status_code == 422
    assert session.calls == []


# ── Session-level errors become 500 ───────────────────────────────────────────

async def test_session_error_becomes_500(client, session) -> None:
    session.result = {"error": "Navigation failed"}
    resp = await client.post(
        "/navigate", json={"url": "https://example.com/"}, headers=AUTH
    )
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Navigation failed"


async def test_raise_if_error_passes_clean_results_through() -> None:
    payload = {"url": "https://example.com/"}
    assert main._raise_if_error(payload) is payload
    assert main._raise_if_error({}) == {}


# ── The lock actually serialises ──────────────────────────────────────────────

async def test_requests_are_serialised_by_the_lock(client, session) -> None:
    """Ten concurrent requests must never overlap inside the session."""
    session.gate = asyncio.Event()
    session.gate.set()  # let each call through as soon as it holds the lock

    responses = await asyncio.gather(
        *(client.get("/state", headers=AUTH) for _ in range(10))
    )

    assert all(r.status_code == 200 for r in responses)
    assert len(session.calls) == 10
    assert session.max_concurrent == 1


async def test_a_slow_request_blocks_the_next_one(client, session) -> None:
    session.gate = asyncio.Event()

    first = asyncio.create_task(client.get("/state", headers=AUTH))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(client.get("/state", headers=AUTH))
    await asyncio.sleep(0.05)

    # The first is parked inside the session; the second cannot have started.
    assert len(session.calls) == 1
    assert not second.done()

    session.gate.set()
    await asyncio.gather(first, second)
    assert len(session.calls) == 2


# ── Route surface ─────────────────────────────────────────────────────────────

def test_no_stealth_route_exists() -> None:
    paths = {r.path for r in main.app.routes if hasattr(r, "path")}
    assert "/stealth" not in paths


def test_expected_routes_are_present() -> None:
    paths = {r.path for r in main.app.routes if hasattr(r, "path")}
    assert {
        "/health", "/state", "/navigate", "/click",
        "/type", "/scroll", "/press_key", "/wait", "/reset",
    } <= paths
