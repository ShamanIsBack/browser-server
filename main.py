from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import re
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlparse

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator

import config
from browser.session import BrowserSession

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_session: BrowserSession | None = None
_lock: asyncio.Lock | None = None

_MIN_API_KEY_LENGTH = 16
_MAX_TYPE_TEXT_LENGTH = 10_000
# Allow single printable chars, named keys (e.g. Enter, ArrowDown, F12), or
# modifier+key combos (e.g. Control+a, Shift+Tab). ASCII-only, and anchored
# with \Z rather than $ — $ also matches just before a trailing newline.
_KEY_PATTERN = re.compile(r"^([A-Za-z]+\+){0,3}([A-Za-z0-9]+|[\x20-\x7e])\Z")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _session, _lock
    _lock = asyncio.Lock()
    _session = BrowserSession()
    await _session.start()
    logger.info("Browser session started (headless=%s)", config.HEADLESS)
    if not config.API_KEY:
        logger.warning(
            "API_KEY is not set — server is running without authentication. "
            "Set API_KEY in .env before deploying."
        )
    elif len(config.API_KEY) < _MIN_API_KEY_LENGTH:
        logger.error(
            "API_KEY is shorter than %d characters — refusing to authenticate any requests. "
            "Set a stronger API_KEY in .env.",
            _MIN_API_KEY_LENGTH,
        )
    yield
    await _session.stop()
    logger.info("Browser session stopped.")


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

_UNAUTHENTICATED_PATHS = {"/health"}


async def _require_api_key(
    request: Request,
    x_api_key: str | None = Security(_api_key_header),
) -> None:
    if request.url.path in _UNAUTHENTICATED_PATHS:
        return
    if not config.API_KEY:
        return  # dev mode — API_KEY not configured, allow all (warning logged at startup)
    if len(config.API_KEY) < _MIN_API_KEY_LENGTH:
        # A too-short key is rejected unconditionally so a weak secret can't be
        # used to authenticate. Startup logs the cause.
        raise HTTPException(status_code=503, detail="Server misconfigured")
    if x_api_key is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    try:
        provided = x_api_key.encode("utf-8")
    except (UnicodeEncodeError, AttributeError):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    if not hmac.compare_digest(provided, config.API_KEY.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


app = FastAPI(
    title="OpenClaw Browser Skill",
    lifespan=_lifespan,
    dependencies=[Depends(_require_api_key)],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _raise_if_error(result: dict) -> dict:
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


def _is_blocked_ip(hostname: str) -> bool:
    """Return True if hostname is a literal IP in a blocked range.

    Domain names (not parseable as IP) are allowed through — no DNS resolution.
    """
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_http_url(v: str) -> str:
    parsed = urlparse(v)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http and https URLs are supported")
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("URL must include a hostname")
    if _is_blocked_ip(hostname):
        raise ValueError(
            "URL hostname resolves to a loopback, private, link-local, "
            "or multicast address — blocked to prevent SSRF"
        )
    return v


# ── Request models ────────────────────────────────────────────────────────────

class NavigateRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        return _validate_http_url(v)

class ClickRequest(BaseModel):
    element_id: int = Field(ge=1, le=10_000)

class TypeRequest(BaseModel):
    element_id: int = Field(ge=1, le=10_000)
    text: str = Field(max_length=_MAX_TYPE_TEXT_LENGTH)

class ScrollRequest(BaseModel):
    direction: Literal["up", "down"]

class PressKeyRequest(BaseModel):
    key: str = Field(min_length=1, max_length=64)

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        if not _KEY_PATTERN.match(v):
            raise ValueError("Key contains unsupported characters")
        return v

class WaitRequest(BaseModel):
    seconds: float = Field(ge=0, le=5.0)

class ResetRequest(BaseModel):
    url: str | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_http_url(v)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/state")
async def get_state():
    async with _lock:
        return _raise_if_error(await _session.get_state())


@app.post("/navigate")
async def navigate(req: NavigateRequest):
    async with _lock:
        return _raise_if_error(await _session.navigate(req.url))


@app.post("/click")
async def click(req: ClickRequest):
    async with _lock:
        return _raise_if_error(await _session.click(req.element_id))


@app.post("/type")
async def type_text(req: TypeRequest):
    async with _lock:
        return _raise_if_error(await _session.type_text(req.element_id, req.text))


@app.post("/scroll")
async def scroll(req: ScrollRequest):
    async with _lock:
        return _raise_if_error(await _session.scroll(req.direction))


@app.post("/press_key")
async def press_key(req: PressKeyRequest):
    async with _lock:
        return _raise_if_error(await _session.press_key(req.key))


@app.post("/wait")
async def wait(req: WaitRequest):
    async with _lock:
        return _raise_if_error(await _session.wait(req.seconds))


@app.post("/reset")
async def reset(req: ResetRequest):
    async with _lock:
        return _raise_if_error(await _session.reset(req.url))


if __name__ == "__main__":
    # workers=1 is required: the browser session and asyncio.Lock are per-process.
    # Multi-worker mode would create N independent browser sessions with no shared state.
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=False, workers=1)
